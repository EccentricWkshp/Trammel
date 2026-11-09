from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ValidationError
from django.http import Http404
from ninja import ModelSchema, Router, Schema, Status

from trammel.api.types import choices
from trammel.core import access, audit
from trammel.customers.models import Customer
from trammel.jobs.models import Job

from . import services
from .models import Invoice, InvoiceLine, InvoiceTax

router = Router(tags=["invoices"])


class InvoiceLineOut(ModelSchema):
    class Meta:
        model = InvoiceLine
        fields = [
            "id",
            "description",
            "quantity",
            "unit",
            "unit_price",
            "discount_pct",
            "line_total",
            "category",
            "taxable",
            "source_type",
            "source_id",
        ]


class TaxOut(ModelSchema):
    class Meta:
        model = InvoiceTax
        fields = ["code", "name", "rate", "base", "amount"]


class InvoiceOut(ModelSchema):
    lines: list[InvoiceLineOut]
    taxes: list[TaxOut]
    is_overdue: bool

    class Meta:
        model = Invoice
        fields = [
            "id",
            "kind",
            "number",
            "customer",
            "job",
            "status",
            "invoice_date",
            "due_date",
            "terms_days",
            "fulfillment",
            "tax_method",
            "manual_jurisdiction",
            "tax_location_code",
            "tax_rate_total",
            "tax_flagged",
            "subtotal",
            "shipping",
            "shipping_taxable",
            "taxable_total",
            "tax_total",
            "total",
            "paid_total",
            "credited_total",
            "written_off",
            "balance",
            "notes",
        ]

    @staticmethod
    def resolve_lines(obj):
        return list(obj.lines.all())

    @staticmethod
    def resolve_taxes(obj):
        return list(obj.taxes.all())

    @staticmethod
    def resolve_is_overdue(obj):
        return obj.is_overdue


class InvoiceCreateIn(Schema):
    customer: UUID | None = None
    job: UUID | None = None
    kind: choices(Invoice.Kind) = Invoice.Kind.FINAL


class InvoiceLineIn(Schema):
    description: str
    quantity: Decimal = Decimal("1")
    unit: str = "each"
    unit_price: Decimal
    discount_pct: Decimal = Decimal("0")
    tax_category: UUID | None = None


class PaymentIn(Schema):
    amount: Decimal
    received_on: dt.date
    method: str = "other"
    reference: str = ""


class VoidIn(Schema):
    reason: str


class InvoiceDraftIn(Schema):
    shipping: Decimal | None = None
    fulfillment: Invoice.Fulfillment | None = None
    #: A jurisdiction id (GET /tax/jurisdictions) to tax at by hand, for when
    #: the address cannot be resolved; null clears it. Issued invoices taxed
    #: this way are flagged for review.
    manual_jurisdiction: UUID | None = None


def _inv(request, perm, pk) -> Invoice:
    return access.get_or_404(request.actor, perm, Invoice.objects.all(), pk=pk)


@router.get("/", response=list[InvoiceOut])
def list_invoices(
    request, status: str | None = None, customer: UUID | None = None, job: UUID | None = None
):
    qs = Invoice.objects.all()
    if status:
        qs = qs.filter(status=status)
    if customer:
        qs = qs.filter(customer_id=customer)
    if job:
        qs = qs.filter(job_id=job)
    return access.scoped(request.actor, "billing.view", qs).order_by("-created_at")[:200]


@router.get("/{invoice_id}", response=InvoiceOut)
def get_invoice(request, invoice_id: UUID):
    return _inv(request, "billing.view", invoice_id)


@router.post("/", response={201: InvoiceOut})
def create(request, payload: InvoiceCreateIn):
    access.require(request.actor, "billing.create")
    job = None
    if payload.job:
        job = access.get_or_404(request.actor, "jobs.view", Job.objects.all(), pk=payload.job)
        customer = job.customer
    elif payload.customer:
        customer = access.get_or_404(
            request.actor, "customers.view", Customer.objects.all(), pk=payload.customer
        )
    else:
        raise ValidationError({"customer": ["A customer or a job is required."]})
    return Status(201, services.create(customer, kind=payload.kind, job=job))


@router.post("/{invoice_id}/lines", response={201: InvoiceOut})
def add_line(request, invoice_id: UUID, payload: InvoiceLineIn):
    invoice = _inv(request, "billing.create", invoice_id)
    data = payload.dict()
    data["tax_category_id"] = data.pop("tax_category")
    services.add_line(invoice, **data)
    invoice.refresh_from_db()
    return Status(201, invoice)


def _set_jurisdiction(invoice: Invoice, jurisdiction_id: UUID | None) -> None:
    from trammel.tax.models import Jurisdiction

    jurisdiction = None
    if jurisdiction_id is not None:
        jurisdiction = Jurisdiction.objects.filter(pk=jurisdiction_id).first()
        if jurisdiction is None:
            raise ValidationError({"manual_jurisdiction": ["No such tax jurisdiction."]})
    before = audit.snapshot(invoice, fields=["manual_jurisdiction"])
    invoice.manual_jurisdiction = jurisdiction
    Invoice.all_objects.filter(pk=invoice.pk).update(manual_jurisdiction=jurisdiction)
    audit.record_update(invoice, before, summary=f"Set the tax jurisdiction on {invoice}")


@router.patch("/{invoice_id}", response=InvoiceOut)
def update_draft(request, invoice_id: UUID, payload: InvoiceDraftIn):
    """Change a draft's fulfillment, shipping charge or hand-chosen tax jurisdiction.

    Totals are recalculated. Fields left out are unchanged; `manual_jurisdiction`
    sent as null returns the invoice to looking its rate up by address.
    """
    invoice = _inv(request, "billing.create", invoice_id)
    if not invoice.is_draft:
        raise ValidationError("Only a draft invoice can be changed.")
    if "manual_jurisdiction" in payload.model_fields_set:
        _set_jurisdiction(invoice, payload.manual_jurisdiction)
    if payload.fulfillment is not None:
        invoice.fulfillment = payload.fulfillment
        Invoice.all_objects.filter(pk=invoice.pk).update(fulfillment=invoice.fulfillment)
    services.set_shipping(
        invoice, invoice.shipping if payload.shipping is None else payload.shipping
    )
    invoice.refresh_from_db()
    return invoice


@router.post("/{invoice_id}/issue", response=InvoiceOut)
def issue(request, invoice_id: UUID):
    return services.issue(_inv(request, "billing.issue", invoice_id), actor=request.actor)


@router.post("/{invoice_id}/void", response=InvoiceOut)
def void(request, invoice_id: UUID, payload: VoidIn):
    invoice = _inv(request, "billing.void", invoice_id)
    services.void(invoice, actor=request.actor, reason=payload.reason)
    invoice.refresh_from_db()
    return invoice


@router.post("/{invoice_id}/payments", response=InvoiceOut)
def pay(request, invoice_id: UUID, payload: PaymentIn):
    invoice = _inv(request, "billing.record_payments", invoice_id)
    if invoice.is_credit_note:
        raise Http404
    services.record_payment(
        invoice.customer,
        amount=payload.amount,
        received_on=payload.received_on,
        method=payload.method,
        reference=payload.reference,
        allocations={invoice: min(payload.amount, invoice.balance)},
        user=request.actor.user,
    )
    invoice.refresh_from_db()
    return invoice
