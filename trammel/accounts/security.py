"""Sign-in rate limiting and lockout (SPEC §9), kept in PostgreSQL — no Redis."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import LoginAttempt

LOCKED_MESSAGE = "Too many failed sign-in attempts. Try again in a few minutes."


def network(ip: str | None) -> str | None:
    """The address that counts for limits: an IPv4 address as it is, an IPv6
    one as its /64, which one household or server usually holds whole, so
    stepping through it isn't a new address each time (SEC-04, 0.10.5)."""
    import ipaddress

    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False).network_address)
    return str(address)


def _window_start():
    return timezone.now() - timedelta(minutes=settings.LOGIN_FAILURE_WINDOW_MINUTES)


def _failures_since_success(**filters) -> int:
    since = _window_start()
    last_success = (
        LoginAttempt.objects.filter(succeeded=True, at__gte=since, **filters)
        .order_by("-at")
        .values_list("at", flat=True)
        .first()
    )
    if last_success is not None:
        since = last_success
    return LoginAttempt.objects.filter(succeeded=False, at__gte=since, **filters).count()


def is_locked(email: str, ip: str | None) -> bool:
    """Sign-in with a password is refused for now.

    Failures count per address *from one IP address*, so someone guessing from
    elsewhere can't lock the real person out; a much higher count per address
    from anywhere still stops a guesser spread over many addresses.
    """
    email = email.strip().lower()
    ip = network(ip)
    if ip is None:
        if _failures_since_success(email=email) >= settings.LOGIN_MAX_FAILURES:
            return True
    elif _failures_since_success(email=email, ip=ip) >= settings.LOGIN_MAX_FAILURES:
        return True
    if _failures_since_success(email=email) >= settings.LOGIN_MAX_FAILURES_PER_ACCOUNT:
        return True
    return ip_locked(ip)


def ip_locked(ip: str | None) -> bool:
    """Too many failed sign-ins from this IP address, for any account."""
    ip = network(ip)
    return bool(
        ip
        and LoginAttempt.objects.filter(ip=ip, succeeded=False, at__gte=_window_start()).count()
        >= settings.LOGIN_MAX_FAILURES_PER_IP
    )


def recently_logged_lock(email: str) -> bool:
    """A refusal for this address is already in the audit log for this window,
    so repeated refusals don't each add a row."""
    from trammel.core.models import AuditEvent

    return AuditEvent.objects.filter(
        action="auth.locked", entity_label=email, at__gte=_window_start()
    ).exists()


def record_attempt(email: str, ip: str | None, succeeded: bool) -> None:
    """A failed password or second factor, or a sign-in completed. A success is
    recorded only once sign-in is complete, second factor and all, so passing
    the password alone never clears the failures counted (SEC-04, 0.10.5)."""
    LoginAttempt.objects.create(
        email=email.strip().lower()[:254], ip=network(ip), succeeded=succeeded
    )


def prune(older_than_days: int = 30) -> int:
    cutoff = timezone.now() - timedelta(days=older_than_days)
    return LoginAttempt.objects.filter(at__lt=cutoff).delete()[0]
