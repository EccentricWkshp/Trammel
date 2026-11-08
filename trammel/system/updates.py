"""The update check (SPEC §4.3): GitHub release tags, never branch HEAD.

Sends nothing but the request. Off when the admin turns it off or when outbound
traffic is switched off. Docker installs are told; they are never updated from
inside the container (that would need the Docker socket — root on the host).
"""

from __future__ import annotations

import json
import re
import urllib.request

from django.conf import settings
from django.utils import timezone

from trammel.core.models import SystemSettings

from .models import UpdateStatus

RELEASES_URL = "https://api.github.com/repos/EccentricWkshp/trammel/releases/latest"


def parse_version(value: str) -> tuple[int, ...]:
    """'v1.2.3' → (1, 2, 3); pre-release and build parts are ignored."""
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    return tuple(int(p) for p in match.groups()) if match else (0, 0, 0)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def fetch_latest(timeout: float = 10) -> tuple[str, str]:
    request = urllib.request.Request(  # noqa: S310 — fixed https URL
        RELEASES_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Trammel/{settings.APP_VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        data = json.loads(response.read())
    return data.get("tag_name", ""), data.get("html_url", "")


def check(fetch=fetch_latest) -> UpdateStatus:
    status = UpdateStatus.load()
    system = SystemSettings.load()
    if not system.update_check_enabled or system.is_offline:
        status.error = "The update check is turned off."
        status.save()
        return status
    try:
        tag, url = fetch()
    except (OSError, ValueError) as exc:
        status.error = f"Could not reach GitHub: {exc}"[:300]
    else:
        status.latest_version, status.release_url, status.error = tag, url, ""
    status.checked_at = timezone.now()
    status.save()
    if (
        status.latest_version
        and is_newer(status.latest_version, settings.APP_VERSION)
        and status.notified_version != status.latest_version
    ):
        try:
            from trammel.notifications.services import notify

            notify(
                "update_available",
                context={
                    "version": status.latest_version,
                    "current": settings.APP_VERSION,
                    "url": status.release_url,
                },
            )
        except ImportError:  # pragma: no cover
            pass
        UpdateStatus.objects.filter(pk=1).update(notified_version=status.latest_version)
    return status


def available() -> str:
    """The newer version to tell the admin about, or ''."""
    status = UpdateStatus.load()
    if status.latest_version and is_newer(status.latest_version, settings.APP_VERSION):
        return status.latest_version
    return ""
