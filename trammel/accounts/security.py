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
    email = email.strip().lower()
    if _failures_since_success(email=email) >= settings.LOGIN_MAX_FAILURES:
        return True
    return bool(
        ip
        and LoginAttempt.objects.filter(ip=ip, succeeded=False, at__gte=_window_start()).count()
        >= settings.LOGIN_MAX_FAILURES_PER_IP
    )


def record_attempt(email: str, ip: str | None, succeeded: bool) -> None:
    LoginAttempt.objects.create(email=email.strip().lower()[:254], ip=ip, succeeded=succeeded)


def prune(older_than_days: int = 30) -> int:
    cutoff = timezone.now() - timedelta(days=older_than_days)
    return LoginAttempt.objects.filter(at__lt=cutoff).delete()[0]
