"""Sign-in rate limiting and lockout (SPEC §9), kept in PostgreSQL — no Redis."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import LoginAttempt

LOCKED_MESSAGE = "Too many failed sign-in attempts. Try again in a few minutes."


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
    LoginAttempt.objects.create(email=email.strip().lower()[:254], ip=ip, succeeded=succeeded)


def prune(older_than_days: int = 30) -> int:
    cutoff = timezone.now() - timedelta(days=older_than_days)
    return LoginAttempt.objects.filter(at__lt=cutoff).delete()[0]
