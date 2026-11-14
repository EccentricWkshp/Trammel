"""Getting back into Trammel when no admin can sign in (FR-ADM-5).

Run on the server, so having a shell there is the proof of trust, as for
backup and restore:

    manage.py recover_admin                      the admin accounts, and any lockouts
    manage.py recover_admin EMAIL                a one-time link to set a new password
    manage.py recover_admin EMAIL --reset-mfa    ... and two-factor sign-in turned off
    manage.py recover_admin --clear-failures     failed sign-ins forgotten, lockouts lifted

Docker: docker compose exec app python manage.py recover_admin ...

Everything it changes is in the audit log, by "System" from the command line.
"""

from __future__ import annotations

import contextlib
import zoneinfo
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from trammel.accounts import mfa, security, services
from trammel.accounts.models import LoginAttempt, User
from trammel.core import audit
from trammel.core.access import SYSTEM


class Command(BaseCommand):
    help = (
        "Get back in when no admin can sign in: list the admin accounts, issue a one-time "
        "link to set an admin's password, turn off their two-factor sign-in, or lift lockouts."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "email", nargs="?", help="The admin to recover. Leave out to list admin accounts."
        )
        parser.add_argument(
            "--reset-mfa",
            action="store_true",
            help="Also turn off their two-factor sign-in (a lost or replaced phone).",
        )
        parser.add_argument(
            "--clear-failures",
            action="store_true",
            help="Forget every recent failed sign-in, lifting any lockout, for every account "
            "and network.",
        )

    def handle(self, *args, email=None, reset_mfa=False, clear_failures=False, **options):
        _business_time_zone()
        with audit.acting_as(SYSTEM, source="cli"):
            if clear_failures:
                self._clear_failures()
            if email:
                self._recover(email, reset_mfa)
            elif reset_mfa:
                raise CommandError("Name the admin whose two-factor sign-in to turn off.")
            elif not clear_failures:
                self._list()

    # -- listing ------------------------------------------------------------------

    def _list(self):
        admins = list(User.objects.filter(is_admin=True).order_by("email"))
        if not admins:
            # Setup makes the first admin, and the last one can't be demoted or
            # deactivated, so this is a database setup hasn't been run on.
            self.stdout.write(
                "There are no admin accounts. Open Trammel in a browser to run the setup page."
            )
            return
        self.stdout.write("Admin accounts:")
        for user in admins:
            seen = f"last signed in {_when(user.last_login)}" if user.last_login else ""
            facts = [
                "active" if user.is_active else "deactivated",
                "two-factor on" if user.totp_confirmed_at else "no two-factor",
                seen or "never signed in",
            ]
            failed = LoginAttempt.objects.filter(
                email=user.email.lower(), succeeded=False, at__gte=_window_start()
            ).count()
            if failed:
                minutes = settings.LOGIN_FAILURE_WINDOW_MINUTES
                facts.append(f"{failed} failed sign-ins in the last {minutes} minutes")
            name = f"  {user.full_name}" if user.full_name else ""
            self.stdout.write(f"  {user.email}{name}  ({', '.join(facts)})")
        locked = _locked_networks()
        if locked:
            self.stdout.write(
                "\nNetworks refused for too many failed sign-ins: " + ", ".join(locked)
            )
            self.stdout.write("  manage.py recover_admin --clear-failures lifts this.")
        self.stdout.write(
            "\nTo set a new password: manage.py recover_admin EMAIL  "
            "(add --reset-mfa if the authenticator is lost too)"
        )

    # -- recovering ---------------------------------------------------------------

    @transaction.atomic
    def _recover(self, email: str, reset_mfa: bool):
        user = User.objects.select_for_update().filter(email__iexact=email.strip()).first()
        if user is None:
            raise CommandError(
                f"No account has the address {email}. "
                "Run manage.py recover_admin without an address to list the admin accounts."
            )
        if not user.is_admin:
            raise CommandError(
                f"{user.email} isn't an admin. Recover an admin, who can then reset this account "
                "from Users."
            )
        if not user.is_active:
            services.set_active(user, True, by=user)
            self.stdout.write(f"Reactivated {user.email}.")
        if reset_mfa and user.totp_confirmed_at:
            mfa.disable(user)
            audit.record(
                "auth.mfa_reset",
                user,
                summary=f"Reset two-factor sign-in for {user} from the command line",
            )
            self.stdout.write(f"Turned off two-factor sign-in for {user.email}.")
        elif user.totp_confirmed_at:
            self.stdout.write(
                "Two-factor sign-in stays on: after the new password, enter a code from the "
                "authenticator or a recovery code (or run this again with --reset-mfa)."
            )
        cleared = LoginAttempt.objects.filter(
            email=user.email.lower(), succeeded=False, at__gte=_window_start()
        ).delete()[0]
        if cleared:
            self.stdout.write(f"Forgot {cleared} recent failed sign-ins for {user.email}.")
        link = services.password_set_link(user, settings.BASE_URL)
        hours = settings.PASSWORD_RESET_TIMEOUT // 3600
        self.stdout.write(
            self.style.SUCCESS(f"\nOpen this link to set a new password for {user.email}:")
        )
        self.stdout.write(f"\n  {link}\n")
        self.stdout.write(
            f"It works once, for {hours} hours, and stops working when the password changes. "
            "Keep it to yourself: anyone with it can set the password."
        )
        if _locked_networks():
            self.stdout.write(
                "Some networks are still refused for failed sign-ins; if yours is one, run "
                "manage.py recover_admin --clear-failures."
            )

    def _clear_failures(self):
        cleared = LoginAttempt.objects.filter(succeeded=False, at__gte=_window_start()).delete()[0]
        if cleared:
            audit.record(
                "auth.failures_cleared",
                entity_type="accounts.loginattempt",
                summary=f"Cleared {cleared} recent failed sign-ins from the command line",
            )
        self.stdout.write(f"Forgot {cleared} recent failed sign-ins; nothing is locked out now.")


def _window_start():
    return timezone.now() - timedelta(minutes=settings.LOGIN_FAILURE_WINDOW_MINUTES)


def _locked_networks() -> list[str]:
    networks = (
        LoginAttempt.objects.filter(succeeded=False, at__gte=_window_start(), ip__isnull=False)
        .values_list("ip", flat=True)
        .distinct()
    )
    return sorted(ip for ip in networks if security.ip_locked(ip))


def _when(value) -> str:
    from trammel.core.templatetags.trammel import dt

    return dt(value)


def _business_time_zone() -> None:
    """Times in the business's zone, as its pages show them."""
    from trammel.core.models import BusinessProfile

    # UTC is fine before setup, or with a zone this server doesn't know.
    with contextlib.suppress(Exception):
        timezone.activate(zoneinfo.ZoneInfo(BusinessProfile.load().timezone))
