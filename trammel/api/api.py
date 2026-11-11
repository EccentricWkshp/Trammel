"""The REST API root, `/api/v1/` (FR-API-1, SPEC §11 versioning).

Each module contributes a router through its manifest; nothing is listed here
by hand. OpenAPI docs are at `/api/v1/docs` for signed-in users.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from ninja import NinjaAPI, Schema
from ninja.throttling import AnonRateThrottle, AuthRateThrottle

from trammel.core import access, units
from trammel.core.lifecycle import TransitionError
from trammel.core.middleware import client_ip
from trammel.core.navigation import mount_api_routers

from .auth import ApiKeyAuth, SessionActorAuth


class _ByClientIP:
    """Counts anonymous calls by the client address Trammel believes, not the
    raw X-Forwarded-For header, which a client can fill with anything."""

    def get_ident(self, request):
        return client_ip(request)


class AnonThrottle(_ByClientIP, AnonRateThrottle):
    pass


class AuthThrottle(_ByClientIP, AuthRateThrottle):
    """Counts each API key, or each signed-in user, on its own."""

    def get_cache_key(self, request):
        auth = getattr(request, "auth", None)
        if auth is not None and getattr(auth, "pk", None) is not None:
            ident = f"{type(auth).__name__}-{auth.pk}"
            return self.cache_format % {"scope": self.scope, "ident": ident}
        return super().get_cache_key(request)


api = NinjaAPI(
    title="Trammel API",
    version="1",
    description="Workshop management for small shops.",
    auth=[ApiKeyAuth(), SessionActorAuth()],
    urls_namespace="api-1",
    docs_decorator=login_required,
    # FR-API-3. Counted in the database cache, so the limit holds across workers.
    throttle=[AuthThrottle(settings.API_RATE_LIMIT), AnonThrottle("30/m")],
)


@api.exception_handler(PermissionDenied)
def _forbidden(request, exc):
    return api.create_response(request, {"detail": str(exc) or "Permission denied."}, status=403)


@api.exception_handler(Http404)
def _not_found(request, exc):
    return api.create_response(request, {"detail": "Not found."}, status=404)


@api.exception_handler(TransitionError)
def _transition(request, exc):
    return api.create_response(request, {"detail": exc.problems}, status=409)


@api.exception_handler(ValidationError)
def _invalid(request, exc):
    detail = exc.message_dict if hasattr(exc, "error_dict") else exc.messages
    return api.create_response(request, {"detail": detail}, status=422)


@api.exception_handler(units.IncompatibleUnits)
def _units(request, exc):
    return api.create_response(request, {"detail": {"unit": [str(exc)]}}, status=422)


class MeOut(Schema):
    kind: str
    id: str | None
    name: str
    is_admin: bool
    permissions: list[str]


@api.get("/me", response=MeOut, tags=["auth"])
def me(request):
    actor = request.actor
    return {
        "kind": actor.kind,
        "id": str(actor.user.pk) if actor.user else None,
        "name": actor.label,
        "is_admin": access.is_admin(actor),
        "permissions": sorted(access.permissions_of(actor)),
    }


mount_api_routers(api)
