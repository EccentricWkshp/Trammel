"""User and role administration (FR-ADM-5, FR-ADM-6). Views and the API both call these."""

from __future__ import annotations

from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from trammel.core import audit

from .models import Role, User


def _active_admins_excluding(user: User) -> int:
    return User.objects.filter(is_admin=True, is_active=True).exclude(pk=user.pk).count()


@transaction.atomic
def save_user(user: User, *, before: dict | None, roles: list[Role] | None = None) -> User:
    """Save a created or edited user, refusing to remove the last active admin."""
    demoting = before is not None and before.get("is_admin") and not user.is_admin
    if demoting and _active_admins_excluding(user) == 0:
        raise ValidationError("At least one active system admin must remain.")
    creating = user._state.adding
    if creating:
        user.set_unusable_password()
    user.full_clean(exclude=["password"])
    user.save()
    old_roles = sorted(str(r) for r in user.roles.all()) if not creating else []
    if roles is not None:
        user.roles.set(roles)
    if creating:
        audit.record_create(user)
    else:
        audit.record_update(user, before or {})
    new_roles = sorted(str(r) for r in user.roles.all())
    if new_roles != old_roles and not creating:
        audit.record(
            "user.roles",
            user,
            summary=f"Changed roles for {user}",
            before={"roles": old_roles},
            after={"roles": new_roles},
        )
    elif creating and new_roles:
        audit.record(
            "user.roles", user, summary=f"Assigned roles to {user}", after={"roles": new_roles}
        )
    return user


@transaction.atomic
def set_active(user: User, active: bool, *, by: User) -> None:
    if user.is_active == active:
        return
    if not active:
        if user.pk == by.pk:
            raise ValidationError("You cannot deactivate your own account.")
        if user.is_admin and _active_admins_excluding(user) == 0:
            raise ValidationError("At least one active system admin must remain.")
    user.is_active = active
    user.deactivated_at = None if active else timezone.now()
    user.save(update_fields=["is_active", "deactivated_at"])
    audit.record(
        "user.activate" if active else "user.deactivate",
        user,
        summary=f"{'Reactivated' if active else 'Deactivated'} {user}",
    )


def password_set_link(user: User, base_url: str) -> str:
    """A one-time link to choose a password — for invites and resets (FR-ADM-5).

    Shown to the admin to pass on, since SMTP may not be configured. The token
    is invalidated by the password changing, so the link works once.
    """
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    audit.record("user.password_link", user, summary=f"Issued a password link for {user}")
    return base_url + reverse("accounts:set_password", args=[uid, token])


@transaction.atomic
def save_role(role: Role, *, before: dict | None) -> Role:
    role.full_clean()
    creating = role._state.adding
    role.save()
    if creating:
        audit.record_create(role)
    else:
        audit.record_update(role, before or {})
    return role


@transaction.atomic
def delete_role(role: Role) -> None:
    holders = role.users.count()
    audit.record(
        "delete",
        role,
        summary=f"Deleted role {role} (held by {holders} user{'s' if holders != 1 else ''})",
        before=audit.snapshot(role),
    )
    role.delete()


def send_magic_link(email: str, ip: str | None) -> None:
    """Email a single-use sign-in link, if `email` belongs to an active account
    and nothing is rate limited. Run by the worker (accounts.magic_link)."""
    import datetime as dt

    from django.conf import settings

    from trammel.core import access
    from trammel.notifications.services import notify

    from . import security
    from .models import MagicLink

    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user is None:
        return
    recent = MagicLink.objects.filter(
        user=user, created_at__gte=timezone.now() - dt.timedelta(minutes=15)
    ).count()
    # Only the IP address's lock applies: a locked password must not also stop
    # the real person signing in by email.
    if recent >= 3 or security.ip_locked(ip):
        return
    token = MagicLink.issue(user, ip=ip)
    link = settings.BASE_URL + reverse("accounts:magic_redeem", args=[token])
    notify(
        "magic_link",
        to=[user.email],
        entity=user,
        context={"link": link, "minutes": MagicLink.LIFETIME_MINUTES},
    )
    audit.record(
        "auth.magic_link",
        user,
        summary=f"Sent a sign-in link to {user}",
        actor=access.ANONYMOUS,
    )
