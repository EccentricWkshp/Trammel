"""How the edge portal takes the built-in portal's place (0.9.x): while it serves
customers, the built-in pages are off and portal links point at it; codes staff
make are hashed for it too."""

from trammel.core import modules, notices, public
from trammel.portal import services as portal


def _elsewhere() -> str:
    if not modules.is_enabled("edge"):
        return ""
    from .models import EdgeSettings

    row = EdgeSettings.load()
    return row.public_url if row.active else ""


def _code_made(row, raw: str) -> None:
    if not modules.is_enabled("edge"):
        return
    from . import client
    from .models import EdgeAccessCode

    EdgeAccessCode.objects.create(code=row, code_hash=client.access_hash(raw))


@notices.register
def sync_failing(request) -> list:
    """Admins hear when the portal on Cloudflare hasn't synced for ten minutes."""
    import datetime as dt

    from django.urls import reverse
    from django.utils import timezone

    from trammel.core import access

    if not access.is_admin(getattr(request, "actor", None)):
        return []
    if not modules.is_enabled("edge"):
        return []
    from trammel.core.templatetags.trammel import dt as when

    from .models import EdgeSettings

    row = EdgeSettings.load()
    stale = timezone.now() - dt.timedelta(minutes=10)
    if not (
        row.active and row.last_error and (row.last_sync_at or row.deployed_at or stale) < stale
    ):
        return []
    since = f" since {when(row.last_sync_at)}" if row.last_sync_at else ""
    return [
        notices.Notice(
            "warning",
            f"The customer portal on Cloudflare hasn't synced{since}: {row.last_error[:200]}",
            reverse("edge:settings"),
        )
    ]


portal.elsewhere.append(_elsewhere)
public.providers.append(_elsewhere)
portal.on_code_made.append(_code_made)
