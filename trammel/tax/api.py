# No `from __future__ import annotations`: the paginated view below must see its
# annotations as real types (see trammel/jobs/api.py).
import datetime as dt
from uuid import UUID

from django.db.models import Q
from django.utils import timezone
from ninja import Field, ModelSchema, Router, Schema
from ninja.pagination import LimitOffsetPagination, paginate

from trammel.core import access

from . import cleanup, services
from .models import ImportBatch, Jurisdiction, TaxRate

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


class ImportOut(ModelSchema):
    class Meta:
        model = ImportBatch
        fields = [
            "id",
            "source",
            "kind",
            "filename",
            "imported_at",
            "rows",
            "skipped",
            "status",
            "error",
            "source_url",
        ]


class RemovedOut(Schema):
    rates: int = 0
    boundaries: int = 0
    jurisdictions: int = 0


class RatesRemoveIn(Schema):
    ids: list[UUID] = Field(..., min_length=1, max_length=5000)
    reason: str = ""


@router.get("/imports", response=list[ImportOut])
@paginate(LimitOffsetPagination)
def imports(request):
    """Rate and boundary file imports, newest first."""
    access.require(request.actor, "tax.view")
    return ImportBatch.objects.all()


@router.delete("/imports/{import_id}", response=RemovedOut)
def remove_import(request, import_id: UUID, reason: str = ""):
    """Remove an import with every rate and postal code range it brought in.

    Imported jurisdictions left with no rate go too. Issued invoices keep the
    tax they were given. Refused while the import is still running.
    """
    batch = access.get_or_404(request.actor, "tax.manage", ImportBatch.objects.all(), pk=import_id)
    return cleanup.remove_batch(batch, reason=reason)


@router.post("/rates/remove", response=RemovedOut)
def remove_rates(request, payload: RatesRemoveIn):
    """Remove tax rates in bulk; imported jurisdictions left with none go too."""
    access.require(request.actor, "tax.manage")
    return cleanup.remove_rates(TaxRate.objects.filter(pk__in=payload.ids), reason=payload.reason)


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
