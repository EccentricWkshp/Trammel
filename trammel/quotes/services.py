"""Quote writes and rendering (FR-QUO). Web views and the API both call these."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from trammel.core import access, audit, numbering, templating
from trammel.core.access import SYSTEM
from trammel.core.models import BusinessProfile, ShopRates
from trammel.core.money import plain_decimal, round_money
from trammel.tax import services as tax
from trammel.tax.models import TaxCategory

from .estimators import ESTIMATORS, Rates
from .models import ZERO, Approval, Quote, QuoteLine, QuoteRevision, QuoteSettings, QuoteTemplate
from .models import Status as S

#: Called with (revision, approval) after a revision is approved — billing
#: registers the deposit invoice here (FR-QUO-9).
on_approved: list[Callable] = []
#: Called with (revision, reason) after a revision is declined.
on_declined: list[Callable] = []
#: Called with (revision,) after a revision is marked sent.
on_sent: list[Callable] = []


# ---------------------------------------------------------------------------
# Quotes and revisions
# ---------------------------------------------------------------------------


def _snapshot_rates(revision: QuoteRevision) -> None:
    rates = ShopRates.load()
    revision.labor_rate = rates.labor_hourly_rate
    revision.overhead_rate = rates.shop_overhead_rate
    revision.material_markup = rates.default_material_markup
    revision.consumables_pct = rates.consumables_allowance
    revision.minimum_charge = rates.minimum_job_charge


#: Job states in which the agreed scope exists, so anything priced is a change order.
AFTER_APPROVAL = ("approved", "in_progress", "completed")


def is_after_approval(job) -> bool:
    """Whether the job's scope has been agreed: approved (or further along), or
    holding an approved quote (a job on hold after approval, say)."""
    return (
        job.status in AFTER_APPROVAL
        or QuoteRevision.objects.filter(
            quote__job=job, quote__kind=Quote.Kind.QUOTE, status=S.APPROVED
        ).exists()
    )


@transaction.atomic
def create_quote(job, *, title: str = "") -> QuoteRevision:
    """A quote, or, once the job is approved, a change order (FR-WO-5)."""
    if job.status in ("closed", "declined", "cancelled"):
        raise ValidationError(f"Job {job.number} is {job.get_status_display().lower()}.")
    change = is_after_approval(job)
    quote = Quote.objects.create(
        job=job,
        kind=Quote.Kind.CHANGE_ORDER if change else Quote.Kind.QUOTE,
        number=numbering.next_number("change_order" if change else "quote"),
        title=title or job.title,
        created_by=audit.current_actor().user,
    )
    settings = QuoteSettings.load()
    revision = QuoteRevision(
        quote=quote,
        number=1,
        template=QuoteTemplate.default(),
        valid_until=timezone.localdate() + dt.timedelta(days=settings.validity_days),
    )
    _snapshot_rates(revision)
    if change:
        revision.minimum_charge = ZERO  # the job's minimum was met by its quote
    revision.save()
    audit.record("create", quote, summary=f"Created {quote.noun} {quote.number} for {job.number}")
    return revision


def can_delete(quote: Quote) -> str:
    """Why `quote` can't be deleted, or "" if it can: only while nothing has
    happened to it, that is none of its revisions has ever been sent."""
    if (
        quote.revisions.exclude(status=S.DRAFT).exists()
        or quote.revisions.filter(sent_at__isnull=False).exists()
    ):
        return (
            f"This {quote.noun} has been sent, so it is kept as a record of what the "
            "customer was offered. It can't be deleted."
        )
    return ""


@transaction.atomic
def delete_quote(quote: Quote, *, reason: str = "") -> None:
    """Move an unsent quote or change order, with its revisions, to the trash."""
    if problem := can_delete(quote):
        raise ValidationError(problem)
    quote.delete()
    audit.record_delete(quote, reason=reason)


@transaction.atomic
def revise(revision: QuoteRevision, *, actor=None) -> QuoteRevision:
    """Start the next revision from this one (FR-QUO-6).

    A sent revision is superseded at once; a declined or expired one keeps its
    status. An approved revision is not revised — scope changes after approval
    are change orders (FR-WO-5).
    """
    if revision.status == S.APPROVED:
        raise ValidationError(
            f"An approved {revision.quote.noun} is changed with a change order, not a revision."
        )
    if revision.status == S.DRAFT:
        raise ValidationError("This revision is still a draft; edit it directly.")
    quote = revision.quote
    latest = quote.revisions.order_by("-number").first()
    if latest.pk != revision.pk and latest.status == S.DRAFT:
        raise ValidationError(f"Revision {latest.number} is already a draft.")
    new = QuoteRevision(
        quote=quote,
        number=latest.number + 1,
        template=revision.template,
        valid_until=timezone.localdate() + dt.timedelta(days=QuoteSettings.load().validity_days),
        description=revision.description,
        customer_notes=revision.customer_notes,
        internal_notes=revision.internal_notes,
        discount_pct=revision.discount_pct,
        deposit_kind=revision.deposit_kind,
        deposit_value=revision.deposit_value,
        fulfillment=revision.fulfillment,
        labor_rate=revision.labor_rate,
        overhead_rate=revision.overhead_rate,
        material_markup=revision.material_markup,
        consumables_pct=revision.consumables_pct,
        minimum_charge=revision.minimum_charge,
        rates_overridden=revision.rates_overridden,
        shipping=revision.shipping,
        amended_terms=revision.amended_terms,
    )
    new.save()
    for line in revision.lines.all():
        line.pk = None
        line.revision = new
        line.save()
    if revision.status == S.SENT:
        QuoteRevision.lifecycle.apply(revision, S.SUPERSEDED, actor=actor or SYSTEM)
    recalculate(new)
    audit.record("quote.revise", quote, summary=f"Started revision {new.number} of {quote.number}")
    return new


def update_revision(revision: QuoteRevision, *, before: dict) -> QuoteRevision:
    if not revision.is_draft:
        raise ValidationError("Only a draft revision can be edited.")
    rate_fields = (
        "labor_rate",
        "overhead_rate",
        "material_markup",
        "consumables_pct",
        "minimum_charge",
    )
    if any(str(getattr(revision, f)) != str(before.get(f)) for f in rate_fields):
        revision.rates_overridden = True
    with transaction.atomic():
        revision.full_clean()
        revision.save()
        recalculate(revision)
        audit.record_update(revision, before)
    return revision


# ---------------------------------------------------------------------------
# Lines and totals
# ---------------------------------------------------------------------------


def _taxable(line: QuoteLine, default: TaxCategory | None) -> bool:
    category = line.tax_category or default
    return True if category is None else category.taxable


def price_line(line: QuoteLine) -> QuoteLine:
    """Compute line_total and cost_total from quantity, prices and discount."""
    line.quantity, line.unit_price, line.unit_cost, line.discount_pct = (
        Decimal(str(v))
        for v in (line.quantity, line.unit_price, line.unit_cost, line.discount_pct or 0)
    )
    gross = line.quantity * line.unit_price
    line.line_total = round_money(gross * (1 - line.discount_pct / 100))
    line.cost_total = round_money(line.quantity * line.unit_cost)
    if line.kind != QuoteLine.Kind.ESTIMATE:
        category = {"labor": "labor", "machine": "machine", "material": "materials"}.get(
            line.kind, "materials" if line.kind == "product" else "other"
        )
        line.split = {category: str(line.line_total)}
    elif line.discount_pct and line.split:
        factor = 1 - line.discount_pct / 100
        line.split = {k: str(round_money(Decimal(v) * factor)) for k, v in line.split.items()}
    return line


@transaction.atomic
def save_line(line: QuoteLine) -> QuoteLine:
    if line.revision.status != S.DRAFT:
        raise ValidationError("Lines can only be changed on a draft revision.")
    if not line.position:
        last = line.revision.lines.order_by("-position").values_list("position", flat=True).first()
        line.position = (last or 0) + 10
    price_line(line)
    line.full_clean(exclude=["revision"])
    line.save()
    recalculate(line.revision)
    return line


@transaction.atomic
def delete_line(line: QuoteLine) -> None:
    revision = line.revision
    line.delete()
    recalculate(revision)


def estimate_line(revision: QuoteRevision, estimator_key: str, data: dict) -> QuoteLine:
    estimator = ESTIMATORS[estimator_key]
    result = estimator.estimate(data, Rates.of(revision))
    return QuoteLine(
        revision=revision,
        kind=QuoteLine.Kind.ESTIMATE,
        description=result.description,
        quantity=result.quantity,
        unit="each",
        unit_price=result.unit_price,
        unit_cost=result.unit_cost,
        estimator=estimator_key,
        inputs=estimator.clean_inputs(data),
        breakdown=result.breakdown,
        split=result.split,
    )


def apply_manual_price(line: QuoteLine, price: Decimal | None, note: str = "") -> QuoteLine:
    """Put a hand-entered unit price on a freshly priced line, or clear one.

    The line arrives priced from its source; that price is kept in
    calculated_unit_price so the two can be compared.
    """
    if price is None:
        line.calculated_unit_price, line.price_note = None, ""
    else:
        line.calculated_unit_price = line.unit_price
        line.unit_price = Decimal(str(price))
        line.price_note = note.strip()
    return line


def save_estimate(line: QuoteLine) -> QuoteLine:
    """Store an estimate line with its exact total (not unit price × quantity).

    With a manual price the total is that price × quantity instead, and the
    split by category (job costing) is scaled to add up to it.
    """
    exact_total = sum((Decimal(r["price"]) for r in line.breakdown), ZERO)
    exact_cost = sum((Decimal(r["cost"]) for r in line.breakdown), ZERO)
    with transaction.atomic():
        save_line(line)
        factor = 1 - line.discount_pct / 100
        if line.price_is_manual:
            split_total = sum((Decimal(v) for v in line.split.values()), ZERO)
            if split_total:
                scale = line.line_total / split_total
                line.split = {
                    k: str(round_money(Decimal(v) * scale)) for k, v in line.split.items()
                }
        else:
            line.line_total = round_money(exact_total * factor)
        line.cost_total = exact_cost
        line.save(update_fields=["line_total", "cost_total", "split"])
        recalculate(line.revision)
    return line


def tax_estimate(revision: QuoteRevision, taxable_base: Decimal) -> tuple[str, Decimal]:
    customer = revision.quote.job.customer
    today = timezone.localdate()
    if customer.exemption_valid_on(today):
        return "Sales tax (exempt)", ZERO
    business = BusinessProfile.load()
    address = customer.default_address("shipping")
    determination = tax.determine(
        fulfillment=revision.fulfillment,
        destination_postal=address.postal_code if address else "",
        shop_postal=business.postal_code,
        on=today,
    )
    if not determination.resolved:
        return "Sales tax, if any, calculated on the invoice", ZERO
    amount = sum((t.amount for t in tax.calculate(taxable_base, determination)), ZERO)
    return determination.short_label, amount


def recalculate(revision: QuoteRevision) -> QuoteRevision:
    """Recompute the totals of a draft revision and store them."""
    if revision.status != S.DRAFT:
        return revision
    lines = list(revision.lines.select_related("tax_category"))
    default = TaxCategory.default()
    lines_total = sum((line.line_total for line in lines), ZERO)
    taxable = sum((line.line_total for line in lines if _taxable(line, default)), ZERO)
    discount = round_money(lines_total * revision.discount_pct / 100)
    subtotal = lines_total - discount
    minimum = (
        max(ZERO, revision.minimum_charge - subtotal)
        if lines and not revision.quote.is_change_order
        else ZERO
    )
    items = subtotal + minimum
    if lines_total:
        taxable = taxable * (1 - revision.discount_pct / 100) + (minimum if taxable else ZERO)
    taxable = round_money(max(taxable, ZERO))
    shipping = revision.shipping or ZERO
    shipping_taxable = tax.taxable_shipping(shipping, taxable, items) if lines else ZERO
    label, tax_amount = tax_estimate(revision, taxable + shipping_taxable) if lines else ("", ZERO)
    revision.lines_total = lines_total
    revision.discount_amount = discount
    revision.minimum_adjustment = minimum
    revision.subtotal = subtotal + minimum
    revision.shipping_taxable = shipping_taxable
    revision.tax_label = label[:120]
    revision.tax_amount = tax_amount
    revision.total = revision.subtotal + shipping + tax_amount
    revision.cost_total = sum((line.cost_total for line in lines), ZERO)
    QuoteRevision.objects.filter(pk=revision.pk).update(
        lines_total=revision.lines_total,
        discount_amount=discount,
        minimum_adjustment=minimum,
        subtotal=revision.subtotal,
        shipping_taxable=shipping_taxable,
        tax_label=revision.tax_label,
        tax_amount=tax_amount,
        total=revision.total,
        cost_total=revision.cost_total,
    )
    return revision


# ---------------------------------------------------------------------------
# Send, approve, decline, expire
# ---------------------------------------------------------------------------


def _move_job(job, target: str, expected: str) -> None:
    from trammel.jobs.models import Job

    job.refresh_from_db()
    if job.status == expected:
        Job.lifecycle.apply(job, target, actor=SYSTEM)


@transaction.atomic
def send(revision: QuoteRevision, *, actor) -> QuoteRevision:
    """Mark the revision sent; it becomes immutable (FR-QUO-6)."""
    recalculate(revision)
    QuoteRevision.lifecycle.apply(revision, S.SENT, actor=actor)
    # Any other open revision of this quote is superseded by this one.
    for other in revision.quote.revisions.filter(status=S.SENT).exclude(pk=revision.pk):
        QuoteRevision.lifecycle.apply(other, S.SUPERSEDED, actor=SYSTEM)
    if not revision.quote.is_change_order:
        _move_job(revision.quote.job, "quoted", "proposal")
    for fn in on_sent:
        fn(revision)
    return revision


def snapshot(revision: QuoteRevision) -> dict:
    return {
        "quote": revision.quote.number,
        "kind": revision.quote.kind,
        "revision": revision.number,
        "valid_until": str(revision.valid_until),
        "description": revision.description,
        "totals": {
            k: str(getattr(revision, k))
            for k in (
                "lines_total",
                "discount_amount",
                "minimum_adjustment",
                "subtotal",
                "shipping",
                "tax_amount",
                "total",
            )
        },
        "tax_label": revision.tax_label,
        "amended_terms": revision.amended_terms,
        "deposit": {
            "kind": revision.deposit_kind,
            "value": str(revision.deposit_value),
            "amount": str(revision.deposit_amount),
        },
        "lines": [
            {
                "description": line.description,
                "quantity": str(line.quantity),
                "unit": line.unit,
                "unit_price": str(line.unit_price),
                "discount_pct": str(line.discount_pct),
                "line_total": str(line.line_total),
            }
            for line in revision.lines.all()
        ],
    }


@transaction.atomic
def approve(
    revision: QuoteRevision,
    *,
    actor,
    approved_by_name: str,
    how: str = "",
    approved_at=None,
    notes: str = "",
    accept_agreement: bool = False,
    method: str = Approval.Method.MANUAL,
    agreement_method: str = "manual",
    ip=None,
    user=None,
    signature: bytes = b"",
) -> Approval:
    """Approve the quote and, with it, the customer agreement (FR-QUO-8, §6 step 5).

    A change order is approved without the customer agreement, which the job
    already has; its amended terms, if it carries any, are accepted with it
    (FR-AGR-6). It never moves the job's status.

    A customer approves their own in the portal (FR-PRT-3), accepting the
    agreement there by `agreement_method` "click", from `ip`.
    """
    from trammel.agreements import services as agreements

    quote = revision.quote
    noun = quote.noun
    actor = access.as_actor(actor)
    perm = "quotes.respond" if actor.is_customer else approve_permission(quote)
    if not access.has_perm(actor, perm, revision):
        raise PermissionDenied(f"Your role does not allow approving {noun}s.")
    if not approved_by_name.strip():
        raise ValidationError(f"Who approved the {noun}?")
    if (
        revision.status == S.SENT
        and revision.valid_until
        and revision.valid_until < timezone.localdate()
    ):
        raise ValidationError(f"This {noun} has expired. Create a new revision to re-quote.")
    job = quote.job
    if quote.is_change_order:
        QuoteRevision.lifecycle.apply(revision, S.APPROVED, actor=actor)
        agreement = None
        if revision.amended_terms.strip():
            issued = agreements.issue(job, primary=False)
            issued.text = f"Amended terms — {quote.number}\n\n{revision.amended_terms.strip()}"
            issued.save(update_fields=["text"])
            agreement = agreements.accept(
                issued,
                method=agreement_method,
                accepted_by_name=approved_by_name,
                how=how,
                accepted_at=approved_at,
                notes=f"Accepted with change order {quote.number}",
                ip=ip,
                user=user,
                signature=signature,
            )
        return _record_approval(
            revision,
            agreement=agreement,
            method=method,
            how=how,
            approved_by_name=approved_by_name,
            approved_at=approved_at,
            notes=notes,
            user=user,
            signature=signature,
        )
    agreement = agreements.accepted(job)
    if agreement is None and not agreements.satisfied(job) and not accept_agreement:
        raise ValidationError(
            "The customer agreement must be accepted with the quote. Confirm that the "
            "customer accepted the terms, or waive the agreement for this job."
        )
    QuoteRevision.lifecycle.apply(revision, S.APPROVED, actor=actor)
    if agreement is None and accept_agreement:
        issued = job.agreements.filter(is_primary=True, status="issued").first()
        if issued is None:
            issued = agreements.issue(
                job,
                deposit_percent=(
                    revision.deposit_value if revision.deposit_kind == "percent" else None
                ),
            )
        agreement = agreements.accept(
            issued,
            method=agreement_method,
            accepted_by_name=approved_by_name,
            how=how,
            accepted_at=approved_at,
            notes=f"Accepted with quote {revision}",
            ip=ip,
            user=user,
            signature=signature,
        )
    approval = _record_approval(
        revision,
        agreement=agreement,
        method=method,
        how=how,
        approved_by_name=approved_by_name,
        approved_at=approved_at,
        notes=notes,
        user=user,
        move_job=True,
        signature=signature,
    )
    return approval


def can_answer(revision: QuoteRevision) -> bool:
    """Sent and still valid: the customer can approve or decline it."""
    valid = revision.valid_until
    return revision.status == S.SENT and not (valid and valid < timezone.localdate())


def terms_to_accept(revision: QuoteRevision) -> dict | None:
    """The terms a customer accepts by approving this quote or change order, as
    {"title", "text", "hash"} — the exact text approve() will record — or None.

    A change order's amended terms, if it carries any; a quote's customer
    agreement, unless the job's agreement is already accepted, waived or not
    required. The hash lets a page check, when the customer presses the
    button, that the text they read is still the text being accepted.
    """
    import hashlib

    from trammel.agreements import services as agreements

    def found(title: str, text: str) -> dict:
        return {"title": title, "text": text, "hash": hashlib.sha256(text.encode()).hexdigest()}

    if revision.quote.is_change_order:
        terms = revision.amended_terms.strip()
        if not terms:
            return None
        return found("Amended terms", f"Amended terms — {revision.quote.number}\n\n{terms}")
    job = revision.quote.job
    if agreements.satisfied(job):
        return None
    issued = job.agreements.filter(is_primary=True, status="issued").first()
    if issued:
        return found("Customer agreement", issued.text)
    percent = revision.deposit_value if revision.deposit_kind == "percent" else None
    return found("Customer agreement", agreements.preview_text(job, deposit_percent=percent))


def approve_permission(quote: Quote) -> str:
    return "quotes.approve_change_orders" if quote.is_change_order else "quotes.approve"


def _record_approval(
    revision: QuoteRevision,
    *,
    agreement,
    method,
    how,
    approved_by_name,
    approved_at,
    notes,
    user,
    move_job: bool = False,
    signature: bytes = b"",
) -> Approval:
    approval = Approval.objects.create(
        signature=signature or b"",
        revision=revision,
        method=method,
        how=how,
        approved_by_name=approved_by_name.strip(),
        approved_at=approved_at or timezone.now(),
        recorded_by=user,
        notes=notes,
        snapshot=snapshot(revision),
        agreement=agreement,
        agreement_text=agreement.text if agreement else "",
    )
    audit.record(
        "quote.approve",
        revision.quote,
        summary=f"{approval.approved_by_name} approved {revision}",
        after={"total": str(revision.total), "how": how},
    )
    if move_job:
        _move_job(revision.quote.job, "approved", "quoted")
    for fn in on_approved:
        fn(revision, approval)
    return approval


@transaction.atomic
def decline(revision: QuoteRevision, *, actor, reason: str = "") -> None:
    QuoteRevision.lifecycle.apply(revision, S.DECLINED, actor=actor, reason=reason)
    for fn in on_declined:
        fn(revision, reason)


def expire_due(today: dt.date | None = None) -> int:
    """Sent revisions past their validity become expired (FR-QUO-7)."""
    today = today or timezone.localdate()
    count = 0
    for revision in QuoteRevision.objects.filter(status=S.SENT, valid_until__lt=today):
        QuoteRevision.lifecycle.apply(
            revision, S.EXPIRED, actor=SYSTEM, reason="Past its validity date"
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Rendering (FR-QUO-5)
# ---------------------------------------------------------------------------


def document_context(revision: QuoteRevision) -> dict:
    from trammel.agreements import services as agreements

    job = revision.quote.job
    business = BusinessProfile.load()
    change = revision.quote.is_change_order
    if change:
        # A change order carries its own amended terms, if any, not the job's agreement.
        agreement_text = revision.amended_terms.strip()
    else:
        agreement = (
            agreements.accepted(job)
            or job.agreements.filter(is_primary=True, status="issued").first()
        )
        agreement_text = agreement.text if agreement else ""
    return {
        "business": templating.business_context(business),
        "customer": templating.customer_context(job.customer),
        "job": {"number": job.number, "title": job.title},
        "quote": {
            "document_title": revision.quote.get_kind_display(),
            "is_change_order": change,
            "number": revision.quote.number,
            "title": revision.quote.title,
            "revision": revision.number,
            "status": revision.get_status_display(),
            "date": (revision.sent_at or revision.created_at).date(),
            "valid_until": revision.valid_until,
            "description": revision.description,
            "notes": revision.customer_notes,
            "lines": [
                {
                    "description": line.description,
                    "quantity": plain_decimal(line.quantity),
                    "unit": line.get_unit_display(),
                    "unit_price": line.unit_price,
                    "discount_pct": line.discount_pct,
                    "total": line.line_total,
                }
                for line in revision.lines.all()
            ],
            "lines_total": revision.lines_total,
            "discount_pct": revision.discount_pct,
            "discount": revision.discount_amount,
            "minimum_adjustment": revision.minimum_adjustment,
            "subtotal": revision.subtotal,
            "shipping": revision.shipping,
            "shipping_label": revision.shipping_label,
            "fulfillment": revision.get_fulfillment_display(),
            "tax_label": revision.tax_label,
            "tax": revision.tax_amount,
            "total": revision.total,
            "deposit": revision.deposit_amount,
            "deposit_percent": revision.deposit_value
            if revision.deposit_kind == "percent"
            else None,
        },
        "agreement": agreement_text,
        "approval": _approval_context(revision),
    }


def _approval_context(revision: QuoteRevision) -> dict | None:
    """Who approved it, when and how, and their signature as a data: URL (empty
    unless they signed), for the document; None until it is approved."""
    from trammel.core.signature import data_uri

    approval = Approval.objects.filter(revision=revision).first()
    if approval is None:
        return None
    return {
        "name": approval.approved_by_name,
        "date": timezone.localtime(approval.approved_at).date(),
        "method": approval.get_method_display(),
        "how": approval.get_how_display() if approval.how else "",
        "signature": data_uri(bytes(approval.signature)) if approval.signature else "",
    }


def render_html(revision: QuoteRevision) -> str:
    template = revision.template or QuoteTemplate.default()
    if template is None:
        raise templating.TemplateProblem("There is no quote template.")
    return templating.render(template.body, document_context(revision))


def render_pdf(revision: QuoteRevision) -> bytes:
    return templating.render_pdf(render_html(revision))
