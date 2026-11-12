"""How the edge portal takes the built-in portal's place (0.9.x): while it serves
customers, the built-in pages are off and portal links point at it; codes staff
make are hashed for it too."""

from trammel.core import modules, public
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


portal.elsewhere.append(_elsewhere)
public.providers.append(_elsewhere)
portal.on_code_made.append(_code_made)
