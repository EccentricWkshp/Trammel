"""API authentication: personal API keys (Bearer) or the browser session (FR-API-2)."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from ninja.security import HttpBearer, SessionAuth

from trammel.accounts.models import ApiKey
from trammel.core import audit
from trammel.core.middleware import client_ip

#: Writing last_used on every call would turn each read into a write; once a
#: minute is precise enough to answer "is this key still in use?".
_LAST_USED_RESOLUTION = timedelta(minutes=1)


class ApiKeyAuth(HttpBearer):
    def authenticate(self, request, token):
        key = ApiKey.authenticate(token)
        if key is None:
            return None
        now = timezone.now()
        ip = client_ip(request)
        if (
            key.last_used_at is None
            or now - key.last_used_at > _LAST_USED_RESOLUTION
            or (key.last_used_ip != ip)
        ):
            ApiKey.objects.filter(pk=key.pk).update(last_used_at=now, last_used_ip=ip)
        actor = key.actor()
        request.actor = actor
        audit.rebind(actor, source="api")
        return key


class SessionActorAuth(SessionAuth):
    """The signed-in browser session, CSRF-checked for unsafe methods."""

    def authenticate(self, request, key):
        user = super().authenticate(request, key)
        return user if user is not None and user.is_active else None
