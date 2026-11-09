# No `from __future__ import annotations`: the paginated view below must see its
# annotations as real types (see trammel/jobs/api.py).
import datetime as dt

from django.db.models import Q
from django.utils import timezone
from ninja import ModelSchema, Router, Schema
from ninja.pagination import LimitOffsetPagination, paginate

from trammel.core import access

from . import services
from .models import Jurisdiction

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


class JurisdictionOut(ModelSchema):
    class Meta:
        model = Jurisdiction
        fields = ["id", "code", "name", "level", "region"]


@router.get("/jurisdictions", response=list[JurisdictionOut])
@paginate(LimitOffsetPagination)
def jurisdictions(request, q: str = "", region: str = ""):
    """Tax jurisdictions, for choosing one by hand on a draft invoice.

    `q` matches the code or name; `region` is a two-letter state.
    """
    access.require(request.actor, "tax.view")
    rows = Jurisdiction.objects.all()
    if q:
        rows = rows.filter(Q(code__icontains=q) | Q(name__icontains=q))
    if region:
        rows = rows.filter(region__iexact=region)
    return rows


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
