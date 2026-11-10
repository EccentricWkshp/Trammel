"""Invoicing, payments and tax (FR-BIL-1..6, FR-TAX-3..9)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from trammel.core import audit, numbering, templating
from trammel.core.access import SYSTEM
from trammel.core.models import BusinessProfile
from trammel.core.money import plain_decimal, round_money
from trammel.tax import services as tax
from trammel.tax.models import TaxCategory

from .models import (
    OPEN,
    ZERO,
    Allocation,
    Category,
    Invoice,
    InvoiceLine,
    InvoiceTax,
    InvoiceTemplate,
    Payment,
    Status,
    WriteOff,
)

#: Called with (invoice,) after an invoice is issued (notifications).
on_issued: list[Callable] = []
#: Called with (payment,) after a payment is recorded.
on_payment: list[Callable] = []


# ---------------------------------------------------------------------------
# Drafts and lines
# ---------------------------------------------------------------------------


@transaction.atomic
def create(customer, *, kind: str = Invoice.Kind.FINAL, job=None, **fields) -> Invoice:
    if customer.is_internal:
        raise ValidationError("Internal shop work is not invoiced.")
    business = BusinessProfile.load()
    invoice = Invoice(
        customer=customer,
        job=job,
        kind=kind,
        template=InvoiceTemplate.default(),
        terms_days=customer.effective_payment_terms(business),
        ship_to=customer.default_address("shipping"),
        **fields,
    )
    invoice.save()
    audit.record(
        "create",
        invoice,
        summary=f"Started a {invoice.get_kind_display().lower()} for {customer}"
        + (f" ({job.number})" if job else ""),
    )
    return invoice


def _require_draft(invoice: Invoice) -> None:
    if not invoice.is_draft:
        raise ValidationError("Only a draft invoice can be changed.")


def _price(line: InvoiceLine) -> InvoiceLine:
    q, p, d = (Decimal(str(v)) for v in (line.quantity, line.unit_price, line.discount_pct or 0))
    line.quantity, line.unit_price, line.discount_pct = q, p, d
    line.line_total = round_money(q * p * (1 - d / 100))
    return line


@transaction.atomic
def add_line(invoice: Invoice, **fields) -> InvoiceLine:
    _require_draft(invoice)
    last = invoice.lines.aggregate(m=models.Max("position"))["m"] or 0
    line = _price(InvoiceLine(invoice=invoice, position=last + 10, **fields))
    line.save()
    recalculate(invoice)
    return line


@transaction.atomic
def update_line(line: InvoiceLine) -> InvoiceLine:
    _require_draft(line.invoice)
    _price(line).save()
    recalculate(line.invoice)
    return line


@transaction.atomic
def delete_line(line: InvoiceLine) -> None:
    invoice = line.invoice
    _require_draft(invoice)
    line.delete()
    recalculate(invoice)


def _already_billed(source_type: str) -> set:
    """Sources on any invoice that is not void (drafts included, so two drafts do not
    both pull the same work)."""
    return set(
        InvoiceLine.objects.filter(source_type=source_type)
        .exclude(invoice__status=Status.VOID)
        .exclude(invoice__deleted_at__isnull=False)
        .values_list("source_id", flat=True)
    )


@transaction.atomic
def add_from_quote(invoice: Invoice, revision, *, percent: Decimal = Decimal("100")) -> int:
    """Pull the approved quote's lines (FR-BIL-1). `percent` for progress billing."""
    _require_draft(invoice)
    billed = _already_billed("quote_line") if percent == 100 else set()
    count = 0
    factor = percent / 100
    # A change order's lines say which change order they come from.
    prefix = f"{revision.quote.number}: " if revision.quote.is_change_order else ""
    for line in revision.lines.select_related("tax_category"):
        if line.pk in billed:
            continue
        category = next(iter(line.split), "other") if len(line.split) == 1 else "other"
        add_line(
            invoice,
            description=prefix + line.description
            if percent == 100
            else f"{prefix}{line.description} ({percent.normalize():f}% progress)",
            quantity=line.quantity,
            unit=line.unit,
            unit_price=(line.line_total / line.quantity * factor).quantize(Decimal("0.0001"))
            if line.quantity
            else ZERO,
            category=category if category in Category.values else "other",
            tax_category=line.tax_category,
            product=line.product,
            source_type="quote_line" if percent == 100 else "quote_line_progress",
            source_id=line.pk,
        )
        count += 1
    if revision.discount_amount and percent == 100:
        add_line(
            invoice,
            description=f"Discount ({revision.discount_pct.normalize():f}%)",
            quantity=1,
            unit_price=-revision.discount_amount,
            category="other",
            source_type="quote_discount",
            source_id=revision.pk,
        )
    if revision.minimum_adjustment and percent == 100:
        add_line(
            invoice,
            description="Minimum job charge",
            quantity=1,
            unit_price=revision.minimum_adjustment,
            category="other",
            source_type="quote_minimum",
            source_id=revision.pk,
        )
    if revision.shipping and percent == 100 and not _shipping_billed(invoice):
        # Shipping is charged once per job: on the first invoice that bills it.
        invoice.shipping = (invoice.shipping or ZERO) + revision.shipping
        invoice.fulfillment = revision.fulfillment
        Invoice.all_objects.filter(pk=invoice.pk).update(
            shipping=invoice.shipping, fulfillment=invoice.fulfillment
        )
        recalculate(invoice)
    return count


def _shipping_billed(invoice: Invoice) -> bool:
    if not invoice.job_id:
        return False
    return (
        Invoice.objects.filter(job=invoice.job, shipping__gt=0)
        .exclude(status=Status.VOID)
        .exclude(kind=Invoice.Kind.CREDIT_NOTE)
        .exclude(pk=invoice.pk)
        .exists()
    )


@transaction.atomic
def set_shipping(invoice: Invoice, amount: Decimal) -> None:
    _require_draft(invoice)
    if amount < 0:
        raise ValidationError("Shipping cannot be negative.")
    before = invoice.shipping
    invoice.shipping = amount
    Invoice.all_objects.filter(pk=invoice.pk).update(shipping=amount)
    recalculate(invoice)
    audit.record(
        "invoice.shipping",
        invoice,
        summary=f"Set shipping on {invoice} to {amount}",
        before={"shipping": str(before)},
        after={"shipping": str(amount)},
    )


@transaction.atomic
def add_unbilled_time(invoice: Invoice) -> int:
    """Billable time not yet invoiced (FR-TIME-5), one line per entry."""
    from trammel.timetracking.models import TimeEntry

    _require_draft(invoice)
    billed = _already_billed("time_entry")
    count = 0
    for entry in TimeEntry.objects.filter(
        work_order__job=invoice.job, billable=True, ended_at__isnull=False
    ).select_related("user", "machine", "work_order"):
        if entry.pk in billed:
            continue
        who = entry.machine if entry.kind == "machine" else "Labor"
        add_line(
            invoice,
            description=f"{who} — {entry.work_order.number} "
            f"{timezone.localtime(entry.started_at):%Y-%m-%d}"
            + (f": {entry.note}" if entry.note else ""),
            quantity=entry.hours,
            unit="hr",
            unit_price=entry.bill_rate,
            category="machine" if entry.kind == "machine" else "labor",
            source_type="time_entry",
            source_id=entry.pk,
        )
        count += 1
    return count


@transaction.atomic
def add_unbilled_materials(invoice: Invoice) -> int:
    """Billable material use not yet invoiced, at cost plus markup."""
    from trammel.core.models import ShopRates
    from trammel.workorders.models import MaterialUsage

    _require_draft(invoice)
    billed = _already_billed("material_usage")
    default_markup = ShopRates.load().default_material_markup
    count = 0
    for use in MaterialUsage.objects.filter(
        work_order__job=invoice.job, billable=True
    ).select_related("material"):
        if use.pk in billed:
            continue
        markup = (
            use.material.markup
            if use.material and use.material.markup is not None
            else default_markup
        )
        add_line(
            invoice,
            description=f"{use.description} ({use.work_order.number})",
            quantity=use.quantity,
            unit=use.unit,
            unit_price=(use.unit_cost * (1 + markup / 100)).quantize(Decimal("0.0001")),
            category="materials",
            source_type="material_usage",
            source_id=use.pk,
        )
        count += 1
    return count


@transaction.atomic
def add_deposit_credits(invoice: Invoice) -> int:
    """Apply deposits paid on this job to the final invoice (FR-BIL-4)."""
    _require_draft(invoice)
    applied = _already_billed("deposit_application")
    count = 0
    for deposit in Invoice.objects.filter(
        job=invoice.job, kind=Invoice.Kind.DEPOSIT, status__in=[Status.PAID, Status.PARTIALLY_PAID]
    ):
        if deposit.pk in applied or not deposit.paid_total:
            continue
        add_line(
            invoice,
            description=f"Less deposit received ({deposit.number})",
            quantity=1,
            unit_price=-deposit.paid_total,
            category="deposit",
            source_type="deposit_application",
            source_id=deposit.pk,
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Tax and totals
# ---------------------------------------------------------------------------


def _taxable(line: InvoiceLine, default: TaxCategory | None) -> bool:
    if line.category == Category.DEPOSIT:
        return False
    category = line.tax_category or default
    return True if category is None else category.taxable


def determine_tax(invoice: Invoice, on: dt.date) -> tax.Determination:
    if invoice.kind == Invoice.Kind.DEPOSIT:
        return tax.Determination(
            True, method="none", note="Deposits are taxed on the final invoice."
        )
    customer = invoice.customer
    if customer.exemption_valid_on(on):
        return tax.Determination(
            True, method="exempt", note=f"Exempt: {customer.tax_exempt_number}"
        )
    if invoice.manual_jurisdiction_id:
        return tax.manual(invoice.manual_jurisdiction, on)
    business = BusinessProfile.load()
    return tax.determine(
        fulfillment=invoice.fulfillment,
        destination_postal=invoice.ship_to.postal_code if invoice.ship_to else "",
        shop_postal=business.postal_code,
        on=on,
    )


def recalculate(invoice: Invoice, determination: tax.Determination | None = None) -> None:
    """Totals for a draft (a preview) or at issue (with the final determination)."""
    if not invoice.is_draft and determination is None:
        return
    lines = list(invoice.lines.select_related("tax_category"))
    default = TaxCategory.default()
    subtotal = sum((line.line_total for line in lines), ZERO)
    items = max(ZERO, sum((ln.line_total for ln in lines if ln.category != Category.DEPOSIT), ZERO))
    taxable = max(ZERO, sum((ln.line_total for ln in lines if _taxable(ln, default)), ZERO))
    shipping = invoice.shipping or ZERO
    shipping_taxable = (
        ZERO if invoice.is_credit_note else tax.taxable_shipping(shipping, taxable, items)
    )
    taxable += shipping_taxable
    on = invoice.invoice_date or timezone.localdate()
    det = determination or determine_tax(invoice, on)
    tax_total = sum((t.amount for t in tax.calculate(taxable, det)), ZERO) if det.resolved else ZERO
    invoice.subtotal, invoice.taxable_total, invoice.tax_total = subtotal, taxable, tax_total
    invoice.shipping_taxable = shipping_taxable
    invoice.total = subtotal + shipping + tax_total
    invoice.balance = (
        invoice.total - invoice.paid_total - invoice.credited_total - invoice.written_off
    )
    Invoice.all_objects.filter(pk=invoice.pk).update(
        subtotal=subtotal,
        shipping_taxable=shipping_taxable,
        taxable_total=taxable,
        tax_total=tax_total,
        total=invoice.total,
        balance=invoice.balance,
    )


# ---------------------------------------------------------------------------
# Issue, void, credit notes
# ---------------------------------------------------------------------------


@transaction.atomic
def issue(invoice: Invoice, *, actor) -> Invoice:
    """Number, date, tax snapshot, freeze (FR-BIL-2, FR-TAX-7)."""
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    _require_draft(invoice)
    if not invoice.lines.exists():
        raise ValidationError("Add at least one line before issuing.")
    on = invoice.invoice_date or timezone.localdate()
    det = determine_tax(invoice, on)
    if not det.resolved:
        raise ValidationError(
            f"{det.note or 'The tax rate could not be determined.'} Choose a tax jurisdiction "
            "by hand; the invoice will be flagged for review."
        )
    default = TaxCategory.default()
    for line in invoice.lines.select_related("tax_category"):
        category = line.tax_category or default
        line.taxable = _taxable(line, default)
        line.gross_receipts_classification = (
            category.gross_receipts_classification if category else ""
        )
        InvoiceLine.objects.filter(pk=line.pk).update(
            taxable=line.taxable,
            gross_receipts_classification=line.gross_receipts_classification,
        )
    recalculate(invoice, det)
    if det.resolved and det.components:
        taxable = invoice.taxable_total
        for t in tax.calculate(taxable, det):
            InvoiceTax.objects.create(
                invoice=invoice, code=t.code, name=t.name, rate=t.rate, base=t.base, amount=t.amount
            )
    key = "credit_note" if invoice.is_credit_note else "invoice"
    method = (
        det.method
        if det.method in Invoice.TaxMethod.values
        else ("none" if not det.components else "address")
    )
    Invoice.all_objects.filter(pk=invoice.pk).update(
        number=numbering.next_number(key, on),
        invoice_date=on,
        due_date=on + dt.timedelta(days=invoice.terms_days),
        issued_at=timezone.now(),
        issued_by=getattr(actor, "user", None),
        tax_method=method,
        tax_location_code=det.location_code,
        tax_postal_code=det.postal_code,
        tax_rate_total=det.total_rate,
        tax_flagged=det.method == "manual",
        tax_note=det.note[:300],
        exemption_number=invoice.customer.tax_exempt_number if det.method == "exempt" else "",
        ship_to_text="\n".join(invoice.ship_to.lines()) if invoice.ship_to else "",
    )
    invoice.refresh_from_db()
    Invoice.lifecycle.apply(invoice, Status.ISSUED, actor=actor)
    if invoice.is_credit_note:
        _apply_credit_note(invoice, actor)
    else:
        refresh_balance(invoice)
    invoice.refresh_from_db()
    for fn in on_issued:
        fn(invoice)
    return invoice


@transaction.atomic
def void(invoice: Invoice, *, actor, reason: str) -> None:
    """Void an issued invoice with nothing paid against it (FR-BIL-2)."""
    invoice.refresh_from_db()
    if (
        invoice.status != Status.ISSUED
        or invoice.allocations.exists()
        or invoice.write_offs.exists()
    ):
        raise ValidationError(
            "Only an issued invoice with no payments, credits or write-offs "
            "can be voided. Issue a credit note instead."
        )
    if invoice.allocations_made.exists():
        raise ValidationError("This credit note has been applied; it cannot be voided.")
    Invoice.lifecycle.apply(invoice, Status.VOID, actor=actor, reason=reason)
    Invoice.objects.filter(pk=invoice.pk).update(voided_at=timezone.now(), balance=ZERO)


@transaction.atomic
def create_credit_note(invoice: Invoice, *, amount: Decimal, description: str) -> Invoice:
    """A draft credit note against an issued invoice (FR-BIL-2)."""
    if invoice.status not in (*OPEN, Status.PAID) or invoice.is_credit_note:
        raise ValidationError("Credit notes are issued against an issued invoice.")
    if amount <= 0:
        raise ValidationError("Enter the amount to credit.")
    note = create(
        invoice.customer, kind=Invoice.Kind.CREDIT_NOTE, job=invoice.job, credit_for=invoice
    )
    note.fulfillment, note.ship_to = invoice.fulfillment, invoice.ship_to
    note.manual_jurisdiction = invoice.manual_jurisdiction
    note.save()
    add_line(
        note,
        description=description or f"Credit against {invoice.number}",
        quantity=1,
        unit_price=amount,
        category="other",
        source_type="credit",
        source_id=invoice.pk,
    )
    # A credit is not taxed again: the credited amount includes any tax.
    note.lines.update(tax_category=_nontaxable_category())
    recalculate(note)
    return note


def _nontaxable_category() -> TaxCategory:
    category, _ = TaxCategory.objects.get_or_create(
        name="Credit (not taxed)", defaults={"taxable": False}
    )
    return category


def _apply_credit_note(note: Invoice, actor) -> None:
    target = note.credit_for
    amount = min(note.total, max(target.balance, ZERO)) if target else ZERO
    if amount > 0:
        Allocation.objects.create(
            invoice=target, credit_note=note, amount=amount, created_by=getattr(actor, "user", None)
        )
        refresh_balance(target)
    refresh_balance(note)


# ---------------------------------------------------------------------------
# Payments, credit, write-offs
# ---------------------------------------------------------------------------


def refresh_balance(invoice: Invoice) -> None:
    """Recompute paid/credited/balance and move the status to match (§6.1)."""
    invoice.refresh_from_db()
    if invoice.status in (Status.DRAFT, Status.VOID):
        return
    if invoice.is_credit_note:
        used = invoice.allocations_made.aggregate(s=Sum("amount"))["s"] or ZERO
        Invoice.objects.filter(pk=invoice.pk).update(balance=invoice.total - used)
        if invoice.status != Status.PAID and used >= invoice.total:
            Invoice.lifecycle.apply(invoice, Status.PAID, actor=SYSTEM)
        return
    paid = invoice.allocations.filter(payment__isnull=False).aggregate(s=Sum("amount"))["s"] or ZERO
    credited = (
        invoice.allocations.filter(credit_note__isnull=False).aggregate(s=Sum("amount"))["s"]
        or ZERO
    )
    written = invoice.write_offs.aggregate(s=Sum("amount"))["s"] or ZERO
    balance = invoice.total - paid - credited - written
    Invoice.objects.filter(pk=invoice.pk).update(
        paid_total=paid, credited_total=credited, written_off=written, balance=balance
    )
    target = (
        Status.PAID
        if balance <= 0
        else (Status.PARTIALLY_PAID if (paid or credited or written) else Status.ISSUED)
    )
    if target != invoice.status:
        Invoice.lifecycle.apply(invoice, target, actor=SYSTEM)


def customer_credit(customer) -> Decimal:
    """Money received but not applied: overpayments and unused credit notes (FR-BIL-4)."""
    paid = Payment.objects.filter(customer=customer).aggregate(s=Sum("amount"))["s"] or ZERO
    allocated = (
        Allocation.objects.filter(payment__customer=customer).aggregate(s=Sum("amount"))["s"]
        or ZERO
    )
    notes = (
        Invoice.objects.filter(
            customer=customer, kind=Invoice.Kind.CREDIT_NOTE, status__in=[*OPEN, Status.PAID]
        ).aggregate(s=Sum("balance"))["s"]
        or ZERO
    )
    return paid - allocated + notes


def customer_balance(customer) -> Decimal:
    return (
        Invoice.objects.filter(customer=customer, status__in=OPEN)
        .exclude(kind=Invoice.Kind.CREDIT_NOTE)
        .aggregate(s=Sum("balance"))["s"]
        or ZERO
    )


@transaction.atomic
def record_payment(
    customer,
    *,
    amount: Decimal,
    received_on: dt.date,
    method: str,
    reference: str = "",
    notes: str = "",
    allocations: dict | None = None,
    user=None,
) -> Payment:
    """Record a payment and apply it (FR-BIL-4). Whatever is not applied is credit."""
    if not amount:
        raise ValidationError("Enter the amount received.")
    allocations = {inv: Decimal(a) for inv, a in (allocations or {}).items() if a}
    if sum(allocations.values(), ZERO) > amount:
        raise ValidationError("More was applied to invoices than was received.")
    payment = Payment.objects.create(
        customer=customer,
        amount=amount,
        received_on=received_on,
        method=method,
        reference=reference,
        notes=notes,
        recorded_by=user,
    )
    for invoice, share in allocations.items():
        invoice.refresh_from_db()
        if invoice.customer_id != customer.pk or invoice.status not in OPEN:
            raise ValidationError(f"{invoice} is not an open invoice of {customer}.")
        if share > invoice.balance:
            raise ValidationError(f"{share} is more than the {invoice.balance} due on {invoice}.")
        Allocation.objects.create(invoice=invoice, payment=payment, amount=share, created_by=user)
        refresh_balance(invoice)
    audit.record(
        "payment.record",
        payment,
        summary=f"Recorded {payment.get_method_display().lower()} payment of {amount} "
        f"from {customer}",
        after={"allocations": {str(k): str(v) for k, v in allocations.items()}},
    )
    for fn in on_payment:
        fn(payment)
    return payment


@transaction.atomic
def apply_credit(invoice: Invoice, amount: Decimal, *, user=None) -> None:
    """Apply a customer's unapplied payments or credit notes to an invoice."""
    invoice.refresh_from_db()
    if invoice.status not in OPEN:
        raise ValidationError("Credit is applied to an open invoice.")
    available = customer_credit(invoice.customer)
    amount = min(amount, available, invoice.balance)
    if amount <= 0:
        raise ValidationError("There is no credit to apply.")
    remaining = amount
    # Unused credit notes first, then unapplied payments, oldest first.
    for note in Invoice.objects.filter(
        customer=invoice.customer, kind=Invoice.Kind.CREDIT_NOTE, status__in=OPEN, balance__gt=0
    ).order_by("issued_at"):
        share = min(note.balance, remaining)
        Allocation.objects.create(invoice=invoice, credit_note=note, amount=share, created_by=user)
        refresh_balance(note)
        remaining -= share
        if not remaining:
            break
    for payment in Payment.objects.filter(customer=invoice.customer).order_by(
        "received_on", "created_at"
    ):
        if not remaining:
            break
        used = payment.allocations.aggregate(s=Sum("amount"))["s"] or ZERO
        free = payment.amount - used
        if free <= 0:
            continue
        share = min(free, remaining)
        Allocation.objects.create(invoice=invoice, payment=payment, amount=share, created_by=user)
        remaining -= share
    refresh_balance(invoice)
    audit.record("credit.apply", invoice, summary=f"Applied {amount} of credit to {invoice}")


@transaction.atomic
def write_off(
    invoice: Invoice,
    *,
    amount: Decimal | None = None,
    reason: str,
    user=None,
    from_force_closeout: bool = False,
) -> WriteOff:
    """Write off an uncollectable balance (§6.1). A recorded, permissioned action."""
    invoice.refresh_from_db()
    if invoice.status not in OPEN:
        raise ValidationError("Only an open invoice can be written off.")
    if not reason.strip():
        raise ValidationError("A reason is required.")
    amount = invoice.balance if amount is None else amount
    if not (0 < amount <= invoice.balance):
        raise ValidationError(f"Write off between 0 and {invoice.balance}.")
    entry = WriteOff.objects.create(
        invoice=invoice,
        amount=amount,
        reason=reason.strip(),
        recorded_by=user,
        from_force_closeout=from_force_closeout,
    )
    refresh_balance(invoice)
    audit.record(
        "invoice.write_off",
        invoice,
        summary=f"Wrote off {amount} of {invoice}",
        reason=reason.strip(),
    )
    return entry


# ---------------------------------------------------------------------------
# Deposits (FR-QUO-9)
# ---------------------------------------------------------------------------


@transaction.atomic
def create_deposit_invoice(revision, approval=None) -> Invoice | None:
    amount = revision.deposit_amount
    job = revision.quote.job
    if amount <= 0 or job.customer.is_internal:
        return None
    invoice = create(job.customer, kind=Invoice.Kind.DEPOSIT, job=job, source_revision=revision)
    label = (
        f"{revision.deposit_value.normalize():f}% deposit"
        if revision.deposit_kind == "percent"
        else "Deposit"
    )
    add_line(
        invoice,
        description=(
            f"{label} on {revision.quote.noun} {revision.quote.number} (total {revision.total})"
        ),
        quantity=1,
        unit_price=amount,
        category="deposit",
        source_type="deposit",
        source_id=revision.pk,
    )
    return issue(invoice, actor=SYSTEM)


# ---------------------------------------------------------------------------
# Rendering (FR-BIL-3)
# ---------------------------------------------------------------------------


def document_context(invoice: Invoice) -> dict:
    business = BusinessProfile.load()
    return {
        "business": templating.business_context(business),
        "customer": templating.customer_context(invoice.customer),
        "invoice": {
            "number": invoice.number or "DRAFT",
            "kind": invoice.get_kind_display(),
            "is_credit_note": invoice.is_credit_note,
            "credit_for": invoice.credit_for.number if invoice.credit_for else "",
            "status": invoice.get_status_display(),
            "date": invoice.invoice_date or timezone.localdate(),
            "due_date": invoice.due_date,
            "terms_days": invoice.terms_days,
            "job": invoice.job.number if invoice.job else "",
            "job_title": invoice.job.title if invoice.job else "",
            "ship_to": invoice.ship_to_text.splitlines() if invoice.ship_to_text else [],
            "lines": [
                {
                    "description": line.description,
                    "quantity": plain_decimal(line.quantity),
                    "unit": line.get_unit_display(),
                    "unit_price": line.unit_price,
                    "discount_pct": line.discount_pct,
                    "total": line.line_total,
                    "taxable": line.taxable,
                }
                for line in invoice.lines.all()
            ],
            "subtotal": invoice.subtotal,
            "shipping": invoice.shipping,
            "shipping_label": invoice.shipping_label,
            "fulfillment": invoice.get_fulfillment_display(),
            "tax_label": invoice.tax_label if not invoice.is_draft else _draft_tax_label(invoice),
            "tax_rate": invoice.tax_rate_total,
            "taxes": [
                {"name": t.name, "code": t.code, "rate": t.rate, "amount": t.amount}
                for t in invoice.taxes.all()
            ],
            "tax_total": invoice.tax_total,
            "exemption_number": invoice.exemption_number,
            "total": invoice.total,
            "paid": invoice.paid_total + invoice.credited_total,
            "written_off": invoice.written_off,
            "balance": invoice.balance,
            "notes": invoice.notes,
        },
    }


def tax_label_for(det: tax.Determination) -> str:
    """The customer-facing tax line for a draft's determination."""
    if det.method == "exempt":
        return "Sales tax (exempt)"
    return det.short_label if det.resolved and det.components else "Sales tax"


def _draft_tax_label(invoice: Invoice) -> str:
    return tax_label_for(determine_tax(invoice, invoice.invoice_date or timezone.localdate()))


def render_html(invoice: Invoice) -> str:
    template = invoice.template or InvoiceTemplate.default()
    if template is None:
        raise templating.TemplateProblem("There is no invoice template.")
    return templating.render(template.body, document_context(invoice))
