"""Bringing online orders in (FR-IMP-1 to FR-IMP-3, FR-IMP-7).

Each order becomes an issued, paid sale ("Sale / receipt") for its customer,
at the prices, shipping and tax the store charged. The tax is recorded as the
store collected it, attributed to the location Trammel looks up for the
ship-to address; a difference from Trammel's own rates flags the sale for
review. Lines link to a catalog product when the SKU matches; nothing is added
to the catalog.

The store's order id keeps an order from being imported twice. Fetching a date
range again is safe: an order already imported only has its shipping, payment
and cancellation news updated, never its issued sale. An order whose buyer the
store withheld waits for staff to supply a customer (FR-IMP-3).

ShipStation and Shippo pass on other stores' orders. One from a store Trammel
imports itself (`direct`) is never a second sale: it only adds what the
store's own order is missing (carrier, tracking, ship date, and the buyer of
an order waiting for one).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from trammel.core import audit
from trammel.core.access import SYSTEM

from . import origins
from .models import SOURCES, ImportedOrder

ZERO = Decimal("0")
PAID = {"paid", "partially_refunded", "refunded"}
LABELS = SOURCES


@dataclass
class Result:
    created: int = 0
    updated: int = 0
    pending: int = 0
    skipped: int = 0
    #: From a store Trammel imports itself: added to that store's own order...
    merged: int = 0
    #: ...or not imported from that store yet, so left for it.
    elsewhere: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self, label: str) -> str:
        parts = [f"{label}: {self.created} new sale{'s' if self.created != 1 else ''}"]
        if self.updated:
            parts.append(f"{self.updated} already imported, brought up to date")
        if self.pending:
            parts.append(
                f"{self.pending} waiting for a customer (the store withheld the buyer's details)"
            )
        if self.skipped:
            parts.append(f"{self.skipped} not paid, test or canceled, skipped")
        if self.merged:
            parts.append(
                f"{self.merged} from stores Trammel imports itself, their shipping details "
                "added to those orders"
            )
        if self.elsewhere:
            parts.append(
                f"{self.elsewhere} from stores Trammel imports itself but not fetched there "
                "yet, left for them (fetch those stores, then this again)"
            )
        if self.errors:
            parts.append(f"{len(self.errors)} couldn't be imported")
        return "; ".join(parts) + "."


def _d(value) -> Decimal:
    return Decimal(str(value or "0"))


def _date(value) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return timezone.localtime(value).date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value:
        try:
            moment = dt.datetime.fromisoformat(value)
        except ValueError:
            return dt.date.fromisoformat(value[:10])
        return timezone.localtime(moment).date() if timezone.is_aware(moment) else moment.date()
    return None


def _moment(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and value:
        moment = dt.datetime.fromisoformat(value)
        return moment if timezone.is_aware(moment) else timezone.make_aware(moment)
    return None


def import_orders(orders: list[dict], source: str, *, user=None, direct=frozenset()) -> Result:
    """Bring in orders read by a platform's reader. One order failing doesn't
    stop the rest: each is its own savepoint. `direct` names the stores Trammel
    imports itself, whose orders a gathering service mustn't bring in again."""
    result = Result()
    for order in orders:
        label = order.get("order_number") or order.get("external_order_id") or "?"
        try:
            with transaction.atomic():
                outcome = _import_one(order, source, user=user, direct=direct)
        except (ValidationError, ValueError, ArithmeticError) as exc:
            message = " ".join(getattr(exc, "messages", [str(exc)]))
            result.errors.append(f"Order {label}: {message}")
            continue
        setattr(result, outcome, getattr(result, outcome) + 1)
    return result


def _import_one(order: dict, source: str, *, user, direct=frozenset()) -> str:
    external_id = str(order.get("external_order_id") or "")
    if not external_id:
        raise ValidationError("It has no order id.")
    row = (
        ImportedOrder.objects.select_for_update()
        .filter(source=source, external_order_id=external_id)
        .first()
    )
    if row is not None:
        _refresh(row, order, source, user=user)
        return "updated"
    origin = order.get("origin") or ""
    if origin and origin != source and origin in direct:
        own = _own_order(origin, order)
        if own is None:
            return "elsewhere"
        _add_missing(own, order, user=user, via=source)
        return "merged"
    if order.get("test") or order.get("unpaid") or order.get("cancelled_at"):
        return "skipped"
    row = ImportedOrder(source=source, external_order_id=external_id)
    _fill(row, order)
    if order.get("customer_unavailable"):
        row.status = ImportedOrder.Status.PENDING
        row.save()
        audit.record(
            "import.pending",
            entity_type="imports.importedorder",
            entity_id=str(row.pk),
            entity_label=str(row),
            summary=f"{row} is waiting for a customer: the store withheld the buyer's details",
        )
        return "pending"
    row.save()
    customer = find_or_create_customer(
        order.get("customer") or {}, source=source, buyer_id=row.buyer_id
    )
    make_sale(row, customer, user=user)
    return "created"


def _fill(row: ImportedOrder, order: dict) -> None:
    """The order's own facts, from the store, on the record."""
    row.order_number = (order.get("order_number") or "")[:50]
    row.buyer_id = (order.get("buyer_id") or "")[:64]
    row.order_date = _moment(order.get("order_date")) or row.order_date or timezone.now()
    row.total, row.tax, row.shipping = (
        _d(order.get("total")),
        _d(order.get("tax")),
        _d(order.get("shipping")),
    )
    row.financial_status = (order.get("financial_status") or "")[:32]
    row.carrier = (order.get("carrier") or "")[:60]
    row.tracking = (order.get("tracking") or "")[:100]
    row.shipped_on = _date(order.get("shipped_on"))
    row.payload = order


def _refresh(row: ImportedOrder, order: dict, source: str, *, user) -> None:
    """An order seen again: shipping, payment and cancellation news only. An
    issued sale never changes; anything that would is noted for staff."""
    was_cancelled = row.cancelled_at is not None
    status_before = row.financial_status
    if row.status == ImportedOrder.Status.PENDING:
        _fill(row, order)
        row.save()
        if not order.get("customer_unavailable"):
            # The store now shares the buyer (a plan upgrade, say): finish it.
            buyer = order.get("customer") or {}
            customer = find_or_create_customer(buyer, source=source, buyer_id=row.buyer_id)
            make_sale(row, customer, user=user)
        return
    row.carrier = (order.get("carrier") or row.carrier)[:60]
    row.tracking = (order.get("tracking") or row.tracking)[:100]
    row.shipped_on = _date(order.get("shipped_on")) or row.shipped_on
    row.financial_status = (order.get("financial_status") or row.financial_status)[:32]
    notes = [row.problem] if row.problem else []
    cancelled = _moment(order.get("cancelled_at"))
    if cancelled and not was_cancelled:
        row.cancelled_at = cancelled
        notes.append(
            f"Canceled in {LABELS.get(source, source)} after it was imported. Void the sale, "
            "or record a credit note if money was refunded."
        )
    if row.financial_status in {"refunded", "partially_refunded"} and (
        status_before != row.financial_status
    ):
        notes.append(
            f"{'Refunded' if row.financial_status == 'refunded' else 'Partly refunded'} in "
            f"{LABELS.get(source, source)}. Record a credit note for the refund."
        )
    row.problem = "\n".join(n for n in notes if n)
    row.save()


def _own_order(origin: str, order: dict) -> ImportedOrder | None:
    """The order as imported from the store it came from, by its order number
    ("#1001" and "1001" alike) or the store's own id for it."""
    wanted = origins.number(order.get("order_number"))
    if not wanted:
        return None
    return (
        ImportedOrder.objects.select_for_update()
        .filter(source=origin)
        .filter(
            Q(order_number__iexact=wanted)
            | Q(order_number__iexact=f"#{wanted}")
            | Q(external_order_id=wanted)
        )
        .first()
    )


def _add_missing(row: ImportedOrder, order: dict, *, user, via: str) -> None:
    """What a gathering service knows that the store's own order lacks: the
    carrier, tracking number and ship date, and, for an order waiting for a
    customer because the store withheld the buyer, the buyer (which makes its
    sale). Nothing the store said is overwritten, and an issued sale never
    changes."""
    via = LABELS.get(via, via)
    added = []
    for name, limit in (("carrier", 60), ("tracking", 100)):
        if not getattr(row, name) and order.get(name):
            setattr(row, name, str(order[name])[:limit])
            added.append(name)
    if not row.shipped_on and _date(order.get("shipped_on")):
        row.shipped_on = _date(order.get("shipped_on"))
        added.append("ship date")
    row.save()
    if row.status == ImportedOrder.Status.PENDING and not order.get("customer_unavailable"):
        buyer = order.get("customer") or {}
        row.payload = {**row.payload, "customer": buyer, "customer_unavailable": False}
        row.save(update_fields=["payload", "updated_at"])
        customer = find_or_create_customer(buyer, source=row.source, buyer_id=row.buyer_id)
        make_sale(row, customer, user=user)
        added.append("buyer")
    if added:
        audit.record(
            "import.details",
            entity_type="imports.importedorder",
            entity_id=str(row.pk),
            entity_label=str(row),
            summary=f"{row}: {', '.join(added)} added from {via}",
        )


def find_or_create_customer(buyer: dict, *, source: str = "", buyer_id: str = ""):
    """The buyer as a customer: the one an earlier order from the same buyer
    went to (by the store's buyer id), else matched by email (theirs or a
    contact's), else new. An existing customer is never changed, except to add
    a new ship-to address."""
    from trammel.customers.models import Customer

    if source and buyer_id:
        earlier = (
            ImportedOrder.objects.filter(source=source, buyer_id=buyer_id)
            .exclude(customer=None)
            .select_related("customer")
            .order_by("imported_at")
            .first()
        )
        if earlier is not None and earlier.customer.deleted_at is None:
            return earlier.customer
    email = (buyer.get("email") or "").strip()
    if email:
        found = (
            Customer.objects.filter(
                Q(email__iexact=email)
                | Q(contacts__email__iexact=email, contacts__deleted_at__isnull=True)
            )
            .filter(is_internal=False, is_placeholder=False)
            .order_by("created_at")
            .first()
        )
        if found is not None:
            return found
    name = (buyer.get("name") or "").strip() or (buyer.get("company") or "").strip()
    customer = Customer.objects.create(
        kind=Customer.Kind.INDIVIDUAL,
        name=(name or email or "Online customer")[:200],
        company=(buyer.get("company") or "")[:200],
        email=email[:254],
        phone=(buyer.get("phone") or "")[:40],
        notes="Added from an online order.",
    )
    audit.record("create", customer, summary=f"Added customer {customer} from an online order")
    return customer


def ship_to(customer, buyer: dict):
    """The order's address among the customer's, added if it is new; None when
    the order has no street address (a pickup, or one the store withheld)."""
    from trammel.customers.models import Address

    line1 = (buyer.get("line1") or "").strip()
    city = (buyer.get("city") or "").strip()
    if not (line1 and city):
        return None
    postal = (buyer.get("postal_code") or "").strip()
    found = customer.addresses.filter(line1__iexact=line1, postal_code__iexact=postal).first()
    if found:
        return found
    return Address.objects.create(
        customer=customer,
        label="From an online order",
        line1=line1[:200],
        line2=(buyer.get("line2") or "")[:200],
        city=city[:100],
        region=(buyer.get("region") or "")[:100],
        postal_code=postal[:20],
        country=(buyer.get("country") or "US")[:2],
        is_default_shipping=not customer.addresses.exists(),
    )


def _product(sku: str):
    if not sku:
        return None
    from trammel.catalog.models import Product

    return Product.objects.filter(sku__iexact=sku).first()


@transaction.atomic
def make_sale(row: ImportedOrder, customer, *, address=None, user=None):
    """The order's sale: issued at the store's prices and tax, and paid if the
    store took the money."""
    from trammel.billing import services as billing
    from trammel.billing.models import Category, Invoice

    order = row.payload
    label = LABELS.get(row.source, row.source)
    buyer = order.get("customer") or {}
    address = address if address is not None else ship_to(customer, buyer)
    on = _date(order.get("order_date")) or timezone.localdate()
    invoice = billing.create(
        customer,
        kind=Invoice.Kind.DIRECT_SALE,
        invoice_date=on,
        fulfillment=Invoice.Fulfillment.SHIPPING if address else Invoice.Fulfillment.PICKUP,
        shipping=_d(order.get("shipping")),
        notes=(order.get("notes") or "")[:2000],
        internal_notes=f"Imported from {label} order {row.order_number}"
        + (f" (from {order['origin_label']})" if order.get("origin_label") else "")
        + ".",
    )
    # Shipped to the order's address (its tax location), and paid on the day.
    invoice.ship_to = address
    invoice.terms_days = 0
    invoice.save(update_fields=["ship_to", "terms_days", "updated_at"])
    for line in order.get("lines") or []:
        product = _product(line.get("sku", ""))
        title = line.get("title") or "Item"
        if line.get("variant"):
            title = f"{title} — {line['variant']}"
        if line.get("sku"):
            title = f"{title} ({line['sku']})"
        billing.add_line(
            invoice,
            description=title,
            quantity=_d(line.get("quantity")),
            unit_price=_d(line.get("unit_price")),
            category=Category.PRODUCT if product else Category.OTHER,
            product=product,
            tax_category=product.tax_category if product else None,
        )
    discount = _d(order.get("discount"))
    if discount:
        billing.add_line(
            invoice,
            description=f"Discount on the {label} order",
            quantity=Decimal("1"),
            unit_price=-discount,
            category=Category.OTHER,
        )
    actor = user if user is not None else SYSTEM
    invoice = billing.issue(
        invoice,
        actor=actor,
        collected_tax=_d(order.get("tax")),
        collected_by=label,
        remitted_by=order.get("tax_remitted_by") or "",
    )
    problems = []
    store_total = _d(order.get("total"))
    if order.get("taxes_included"):
        problems.append(
            f"{label} prices include tax; the sale shows it on top. Check the total against "
            f"{label}'s {store_total}."
        )
    elif store_total and abs(invoice.total - store_total) > Decimal("0.01"):
        problems.append(
            f"The sale comes to {invoice.total}; {label} charged {store_total}. Check it."
        )
    paid = _paid_amount(order, invoice.total)
    if paid > ZERO:
        billing.record_payment(
            customer,
            amount=paid,
            received_on=on,
            method="online",
            reference=f"{label} {row.order_number}"[:100],
            notes=f"Paid in {label}.",
            allocations={invoice: min(paid, invoice.total)},
            user=user,
        )
    if order.get("financial_status") in {"refunded", "partially_refunded"}:
        problems.append(f"Refunded in {label}: record a credit note for the refund.")
    row.status = ImportedOrder.Status.IMPORTED
    row.customer = customer
    row.invoice = invoice
    row.problem = "\n".join(p for p in [row.problem, *problems] if p)
    row.save()
    audit.record(
        "import.sale",
        invoice,
        summary=f"Imported {label} order {row.order_number} as {invoice.number}",
    )
    return invoice


def _paid_amount(order: dict, total: Decimal) -> Decimal:
    status = order.get("financial_status") or ""
    if status in PAID:
        return total
    if status == "partially_paid":
        return max(ZERO, min(total, _d(order.get("total")) - _d(order.get("outstanding"))))
    return ZERO


# ---------------------------------------------------------------------------
# Orders waiting for a customer
# ---------------------------------------------------------------------------


def complete(row: ImportedOrder, *, customer=None, new: dict | None = None, user=None):
    """Give a waiting order its customer — an existing one, or a new one from
    `new` (name, email, company, phone and address fields) — and make its sale."""
    if row.status != ImportedOrder.Status.PENDING:
        raise ValidationError("This order already has its sale.")
    with transaction.atomic():
        if customer is None:
            new = new or {}
            if not (new.get("name") or "").strip():
                raise ValidationError("Choose a customer, or enter the new customer's name.")
            customer = find_or_create_customer(new)
            address = ship_to(customer, new)
        else:
            address = ship_to(customer, {**(row.payload.get("customer") or {}), **(new or {})})
        return make_sale(row, customer, address=address, user=user)


def discard(row: ImportedOrder) -> None:
    """Drop a waiting order. Fetching its dates again brings it back."""
    if row.status != ImportedOrder.Status.PENDING:
        raise ValidationError("Only an order waiting for a customer can be discarded.")
    audit.record(
        "import.discard",
        entity_type="imports.importedorder",
        entity_id=str(row.pk),
        entity_label=str(row),
        summary=f"Discarded {row}, which was waiting for a customer",
    )
    row.delete()


# ---------------------------------------------------------------------------
# A job for an order that has to be made
# ---------------------------------------------------------------------------


@transaction.atomic
def make_job(row: ImportedOrder, *, user=None):
    """A job for making an imported order, ready for a work order.

    The order is mirrored as a quote at the prices charged, approved as "paid
    online": the buyer accepted the store's terms at checkout, so Trammel's own
    agreement is waived for the job. A work order made from it gets the
    products' routings and bills of materials as usual, and the imported sale
    already bills it.
    """
    from trammel.billing.models import InvoiceLine
    from trammel.core import modules
    from trammel.jobs import services as jobs
    from trammel.jobs.models import Job
    from trammel.quotes import services as quotes
    from trammel.quotes.models import QuoteLine

    if row.invoice is None:
        raise ValidationError("Give the order its customer first.")
    if row.job_id:
        raise ValidationError(f"{row.job.number} was already made for this order.")
    label = row.get_source_display()
    invoice = row.invoice
    job = jobs.create_job(
        Job(
            title=f"{label} order {row.order_number}"[:200],
            customer=invoice.customer,
            description=f"Bought online: {label} order {row.order_number}, sale {invoice.number}.",
        )
    )
    revision = quotes.create_quote(job, title=f"{label} order {row.order_number}")
    revision.deposit_kind = revision.Deposit.NONE
    revision.deposit_value = ZERO
    revision.shipping = invoice.shipping
    revision.description = job.description
    revision.save()
    pairs = []
    for line in invoice.lines.order_by("position"):
        quote_line = quotes.save_line(
            QuoteLine(
                revision=revision,
                kind=QuoteLine.Kind.ADHOC,
                description=line.description,
                quantity=line.quantity,
                unit=line.unit,
                unit_price=line.unit_price,
                product=line.product,
                tax_category=line.tax_category,
            )
        )
        pairs.append((line.pk, quote_line.pk))
    quotes.send(revision, actor=SYSTEM)
    if modules.is_enabled("agreements"):
        from trammel.agreements import services as agreements

        if not agreements.satisfied(job):
            agreements.waive(
                job,
                reason=f"Bought online through {label} (order {row.order_number}); the store's "
                "own terms apply.",
            )
    quotes.approve(
        revision,
        actor=SYSTEM,
        approved_by_name=invoice.customer.name,
        method="online",
        notes=f"Paid online: {label} order {row.order_number}, sale {invoice.number}.",
        user=user,
    )
    # The sale already bills the quote: say so, so it is never invoiced twice.
    for invoice_line, quote_line in pairs:
        InvoiceLine.objects.filter(pk=invoice_line).update(
            source_type="quote_line", source_id=quote_line
        )
    type(invoice).all_objects.filter(pk=invoice.pk).update(job=job)
    row.job = job
    row.save(update_fields=["job", "updated_at"])
    audit.record("import.job", job, summary=f"Made {job.number} from {row}")
    return job


def forget_buyer(source: str, buyer_id: str) -> int:
    """Every order of a buyer, for a platform's deletion request (FR-IMP-6/7):
    the stored copy of their details is cleared. Returns how many orders."""
    rows = ImportedOrder.objects.filter(source=source, buyer_id=buyer_id)
    count = 0
    for row in rows:
        payload = dict(row.payload)
        payload["customer"] = {k: "" for k in (payload.get("customer") or {})}
        row.payload = payload
        row.save(update_fields=["payload", "updated_at"])
        count += 1
    return count
