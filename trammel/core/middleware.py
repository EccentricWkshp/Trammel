from __future__ import annotations

import functools
import ipaddress
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


class HealthCheckMiddleware:
    """Answers GET /healthz first: the container health check calls the app at
    127.0.0.1 over plain HTTP, which neither the allowed host names nor the
    HTTPS redirect would let through. It says only that the database answers."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/healthz" and request.method in ("GET", "HEAD"):
            from django.db import connection
            from django.http import JsonResponse

            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            return JsonResponse({"status": "ok"})
        return self.get_response(request)


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


@functools.cache
def _trusted_networks(entries: tuple[str, ...]):
    """The TRUSTED_PROXIES networks, or None when "*" trusts every sender."""
    if "*" in entries:
        return None
    return tuple(ipaddress.ip_network(entry, strict=False) for entry in entries)


def _trusted(address: str, networks) -> bool:
    if networks is None:
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def client_ip(request) -> str | None:
    """The client address, from X-Forwarded-For only as far as it can be believed.

    The header is read only when the connection comes from a trusted proxy, and
    then from the right: each hop was appended by the proxy before it, so the
    first address that is not itself a trusted proxy is the client. Anything to
    its left was written by the client and could say anything.
    """
    remote = request.META.get("REMOTE_ADDR")
    entries = tuple(settings.TRUSTED_PROXIES)
    if not entries or not remote:
        return remote
    networks = _trusted_networks(entries)
    if not _trusted(remote, networks):
        return remote
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if not hops:
        return remote
    if networks is None:
        # "*" believes the one proxy in front of the app, whatever its address:
        # the address it appended, the rightmost, is the client. Nothing to its
        # left is believed — the client could have written it.
        candidate = hops[-1]
    else:
        # Skip proxies we trust from the right; the first one we don't is the
        # client. If every hop is a trusted proxy, the leftmost of them is.
        candidate = next((hop for hop in reversed(hops) if not _trusted(hop, networks)), hops[0])
    # A hop that isn't an address is never taken, nor skipped past.
    return candidate if _is_ip(candidate) else remote


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


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
