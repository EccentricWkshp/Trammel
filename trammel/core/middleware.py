from __future__ import annotations

import zoneinfo

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from . import audit, formats, modules
from .access import as_actor
from .models import BusinessProfile, SystemSettings

_SETUP_EXEMPT_PREFIXES = ("/setup/", "/static/", "/healthz")


def setup_complete() -> bool:
    return SystemSettings.objects.filter(pk=1, setup_completed_at__isnull=False).exists()


class SetupRequiredMiddleware:
    """Until the setup page has run, every page leads to it (FR-ADM-1)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(_SETUP_EXEMPT_PREFIXES) and not setup_complete():
            if request.path.startswith("/api/"):
                from django.http import JsonResponse

                return JsonResponse({"detail": "Setup has not been completed."}, status=503)
            return redirect(reverse("core:setup"))
        return self.get_response(request)


def client_ip(request) -> str | None:
    """The client address. Proxy headers are believed only when configured to be."""
    if settings.TRUST_PROXY_HEADERS:
        if real := request.META.get("HTTP_X_REAL_IP"):
            return real.strip()
        if forwarded := request.META.get("HTTP_X_FORWARDED_FOR"):
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class AuditContextMiddleware:
    """Binds the acting principal for the audit log and caches module state.

    An API key authenticator rebinds the actor once it knows the key; the reset
    here restores the outer value whatever happened inside.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        actor = as_actor(getattr(request, "user", None))
        request.actor = actor
        source = "api" if request.path.startswith("/api/") else "web"
        cache_token = modules.begin_request_cache()
        try:
            with audit.acting_as(actor, ip=client_ip(request), source=source):
                response = self.get_response(request)
                # FR-API-3: every API call made with a key that changes something is
                # logged with the key, whatever the outcome.
                api_actor = getattr(request, "actor", actor)
                if api_actor.api_key_id and request.method not in ("GET", "HEAD", "OPTIONS"):
                    audit.record(
                        "api.call",
                        entity_type="accounts.apikey",
                        entity_id=str(api_actor.api_key_id),
                        summary=f"{request.method} {request.path} → {response.status_code}",
                        actor=api_actor,
                    )
                return response
        finally:
            modules.end_request_cache(cache_token)


class TimezoneMiddleware:
    """Display times in the user's zone, else the business zone (SPEC §4.4)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.business = BusinessProfile.load()
        name = getattr(getattr(request, "user", None), "timezone", "") or request.business.timezone
        try:
            timezone.activate(zoneinfo.ZoneInfo(name))
        except zoneinfo.ZoneInfoNotFoundError, ValueError:
            timezone.deactivate()
        format_token = formats.activate(request.business.date_format)
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
            formats.deactivate(format_token)
