from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ValidationError
from django.http import Http404
from ninja import ModelSchema, Router, Schema, Status

from trammel.core import access
from trammel.jobs.models import Job

from . import services
from .estimators import ESTIMATORS
from .forms import LINE_FORMS
from .models import QuoteLine, QuoteRevision

router = Router(tags=["quotes"])


class QuoteLineOut(ModelSchema):
    #: The estimator's rows ({label, detail, cost, price}); each row's cost is
    #: left out without permission to see costs (core.see_costs).
    breakdown: list[dict]

    class Meta:
        model = QuoteLine
        fields = [
            "id",
            "position",
            "kind",
            "description",
            "quantity",
            "unit",
            "unit_price",
            "discount_pct",
            "line_total",
            "estimator",
            "inputs",
            "split",
            "offer",
            "group",
            "included",
        ]

    @staticmethod
    def resolve_breakdown(obj, context):
        rows = [row for row in (obj.breakdown or []) if isinstance(row, dict)]
        if access.sees_costs(context):
            return rows
        hidden = {"cost", "unit_cost", "total_cost", "margin"}
        return [{k: v for k, v in row.items() if k not in hidden} for row in rows]


class RevisionOut(ModelSchema):
    quote_number: str
    #: "quote", or "change_order" for anything priced after the job was approved.
    kind: str
    job: UUID
    lines: list[QuoteLineOut]

    class Meta:
        model = QuoteRevision
        fields = [
            "id",
            "number",
            "status",
            "valid_until",
            "description",
            "customer_notes",
            "amended_terms",
            "discount_pct",
            "deposit_kind",
            "deposit_value",
            "fulfillment",
            "labor_rate",
            "overhead_rate",
            "material_markup",
            "consumables_pct",
            "minimum_charge",
            "lines_total",
            "discount_amount",
            "minimum_adjustment",
            "subtotal",
            "shipping",
            "shipping_taxable",
            "tax_label",
            "tax_amount",
            "total",
            "sent_at",
            "created_at",
        ]

    @staticmethod
    def resolve_quote_number(obj):
        return obj.quote.number

    @staticmethod
    def resolve_kind(obj):
        return obj.quote.kind

    @staticmethod
    def resolve_job(obj):
        return obj.quote.job_id

    @staticmethod
    def resolve_lines(obj):
        return list(obj.lines.all())


class QuoteCreateIn(Schema):
    job: UUID
    title: str = ""


class QuoteLineIn(Schema):
    kind: str
    data: dict


class EstimateIn(Schema):
    estimator: str
    inputs: dict


class ApproveIn(Schema):
    approved_by_name: str
    how: str = "other"
    approved_at: dt.datetime | None = None
    notes: str = ""
    accept_agreement: bool = False
    #: The alternatives and options the customer chose (line ids); leave out to
    #: keep the defaults.
    picks: list[UUID] | None = None


class OfferIn(Schema):
    #: "included", "optional", or "alternate".
    offer: str
    #: An alternative's group, such as "Wood".
    group: str = ""
    #: Make this alternative its group's choice until the customer picks.
    default: bool = False


class DeclineIn(Schema):
    reason: str = ""


class QuoteDraftIn(Schema):
    #: What is being done, in narrative terms.
    description: str | None = None
    shipping: Decimal | None = None
    fulfillment: QuoteRevision.Fulfillment | None = None
    #: Change orders only: changed terms accepted with it.
    amended_terms: str | None = None


def _rev(request, perm, pk) -> QuoteRevision:
    return access.get_or_404(
        request.actor, perm, QuoteRevision.objects.select_related("quote__job"), pk=pk
    )


def _form_errors(form) -> ValidationError:
    return ValidationError({k: [str(e) for e in v] for k, v in form.errors.items()})


@router.get("/", response=list[RevisionOut])
def list_quotes(
    request, job: UUID | None = None, status: str | None = None, kind: str | None = None
):
    qs = QuoteRevision.objects.select_related("quote")
    if job:
        qs = qs.filter(quote__job_id=job)
    if kind:
        qs = qs.filter(quote__kind=kind)
    if status:
        qs = qs.filter(status=status)
    return access.scoped(request.actor, "quotes.view", qs).order_by("-created_at")[:200]


@router.get("/{revision_id}", response=RevisionOut)
def get_revision(request, revision_id: UUID):
    return _rev(request, "quotes.view", revision_id)


@router.post("/", response={201: RevisionOut})
def create(request, payload: QuoteCreateIn):
    """A quote, or a change order when the job has already been approved."""
    access.require(request.actor, "quotes.create")
    job = access.get_or_404(request.actor, "jobs.view", Job.objects.all(), pk=payload.job)
    return Status(201, services.create_quote(job, title=payload.title))


@router.post("/{revision_id}/lines", response={201: RevisionOut})
def add_line(request, revision_id: UUID, payload: QuoteLineIn):
    revision = _rev(request, "quotes.edit", revision_id)
    form_class = LINE_FORMS.get(payload.kind)
    if form_class is None:
        raise ValidationError({"kind": [f"One of: {', '.join(LINE_FORMS)}"]})
    form = form_class(payload.data)
    if revision.quote.is_change_order and hasattr(form, "allow_credit"):
        form.allow_credit()
    # New catalog products are added through the catalog API, not with a line.
    getattr(form, "drop_new_product", lambda: None)()
    if not form.is_valid():
        raise _form_errors(form)
    services.save_line(form.build(revision))
    revision.refresh_from_db()
    return Status(201, revision)


@router.post("/{revision_id}/estimates", response={201: RevisionOut})
def add_estimate(request, revision_id: UUID, payload: EstimateIn):
    revision = _rev(request, "quotes.edit", revision_id)
    estimator = ESTIMATORS.get(payload.estimator)
    if estimator is None:
        raise ValidationError({"estimator": [f"One of: {', '.join(ESTIMATORS)}"]})
    form = estimator.form_class()(payload.inputs)
    if not form.is_valid():
        raise _form_errors(form)
    services.save_estimate(services.estimate_line(revision, payload.estimator, form.cleaned_data))
    revision.refresh_from_db()
    return Status(201, revision)


@router.delete("/{revision_id}/lines/{line_id}", response=RevisionOut)
def delete_line(request, revision_id: UUID, line_id: UUID):
    revision = _rev(request, "quotes.edit", revision_id)
    line = revision.lines.filter(pk=line_id).first()
    if line is None:
        raise Http404
    services.delete_line(line)
    revision.refresh_from_db()
    return revision


@router.post("/{revision_id}/lines/{line_id}/offer", response=RevisionOut)
def offer_line(request, revision_id: UUID, line_id: UUID, payload: OfferIn):
    """Offer a draft's line as included, optional, or an alternative (FR-QUO-11)."""
    revision = _rev(request, "quotes.edit", revision_id)
    line = revision.lines.filter(pk=line_id).first()
    if line is None:
        raise Http404
    services.set_offer(line, payload.offer, payload.group, payload.default)
    revision.refresh_from_db()
    return revision


@router.patch("/{revision_id}", response=RevisionOut)
def update_draft(request, revision_id: UUID, payload: QuoteDraftIn):
    """Change a draft revision's description, fulfillment, shipping charge or amended
    terms."""
    from trammel.core import audit

    revision = _rev(request, "quotes.edit", revision_id)
    if payload.amended_terms is not None and not revision.quote.is_change_order:
        raise ValidationError({"amended_terms": ["Only a change order carries amended terms."]})
    before = audit.snapshot(revision)
    for field in ("description", "shipping", "fulfillment", "amended_terms"):
        if (value := getattr(payload, field)) is not None:
            setattr(revision, field, value)
    services.update_revision(revision, before=before)
    revision.refresh_from_db()
    return revision


@router.post("/{revision_id}/send", response=RevisionOut)
def send(request, revision_id: UUID):
    revision = _rev(request, "quotes.edit", revision_id)
    services.send(revision, actor=request.actor)
    revision.refresh_from_db()
    return revision


@router.post("/{revision_id}/approve", response=RevisionOut)
def approve(request, revision_id: UUID, payload: ApproveIn):
    revision = _rev(request, "quotes.view", revision_id)
    access.require(request.actor, services.approve_permission(revision.quote), revision)
    services.approve(revision, actor=request.actor, user=request.actor.user, **payload.dict())
    revision.refresh_from_db()
    return revision


@router.post("/{revision_id}/decline", response=RevisionOut)
def decline(request, revision_id: UUID, payload: DeclineIn):
    revision = _rev(request, "quotes.edit", revision_id)
    services.decline(revision, actor=request.actor, reason=payload.reason)
    revision.refresh_from_db()
    return revision


@router.post("/{revision_id}/revise", response={201: RevisionOut})
def revise(request, revision_id: UUID):
    revision = _rev(request, "quotes.edit", revision_id)
    return Status(201, services.revise(revision, actor=request.actor))
