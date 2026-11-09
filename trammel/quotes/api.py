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
            "breakdown",
            "split",
        ]


class RevisionOut(ModelSchema):
    quote_number: str
    job: UUID
    lines: list[QuoteLineOut]

    class Meta:
        model = QuoteRevision
        fields = [
            "id",
            "number",
            "status",
            "valid_until",
            "customer_notes",
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


class DeclineIn(Schema):
    reason: str = ""


class QuoteDraftIn(Schema):
    shipping: Decimal | None = None
    fulfillment: QuoteRevision.Fulfillment | None = None


def _rev(request, perm, pk) -> QuoteRevision:
    return access.get_or_404(
        request.actor, perm, QuoteRevision.objects.select_related("quote__job"), pk=pk
    )


def _form_errors(form) -> ValidationError:
    return ValidationError({k: [str(e) for e in v] for k, v in form.errors.items()})


@router.get("/", response=list[RevisionOut])
def list_quotes(request, job: UUID | None = None, status: str | None = None):
    qs = QuoteRevision.objects.select_related("quote")
    if job:
        qs = qs.filter(quote__job_id=job)
    if status:
        qs = qs.filter(status=status)
    return access.scoped(request.actor, "quotes.view", qs).order_by("-created_at")[:200]


@router.get("/{revision_id}", response=RevisionOut)
def get_revision(request, revision_id: UUID):
    return _rev(request, "quotes.view", revision_id)


@router.post("/", response={201: RevisionOut})
def create(request, payload: QuoteCreateIn):
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


@router.patch("/{revision_id}", response=RevisionOut)
def update_draft(request, revision_id: UUID, payload: QuoteDraftIn):
    """Change a draft revision's fulfillment or shipping charge."""
    from trammel.core import audit

    revision = _rev(request, "quotes.edit", revision_id)
    before = audit.snapshot(revision)
    for field in ("shipping", "fulfillment"):
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
    revision = _rev(request, "quotes.approve", revision_id)
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
