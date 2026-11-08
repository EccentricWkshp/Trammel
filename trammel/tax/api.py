from __future__ import annotations

import datetime as dt

from django.utils import timezone
from ninja import Router, Schema

from trammel.core import access

from . import services

router = Router(tags=["tax"])


class ComponentOut(Schema):
    code: str
    name: str
    rate: str


class LookupOut(Schema):
    resolved: bool
    postal_code: str
    location_code: str
    total_rate: str
    components: list[ComponentOut]
    note: str


@router.get("/lookup", response=LookupOut)
def lookup(request, postal_code: str, on: dt.date | None = None):
    """The sales tax rate for a ZIP or ZIP+4 on a date (FR-TAX-6). No outbound call."""
    access.require(request.actor, "tax.view")
    d = services.lookup_postal(postal_code, on or timezone.localdate())
    return {
        "resolved": d.resolved,
        "postal_code": d.postal_code,
        "location_code": d.location_code,
        "total_rate": str(d.total_rate),
        "components": [{"code": c.code, "name": c.name, "rate": str(c.rate)} for c in d.components],
        "note": d.note,
    }
