"""Reports (FR-RPT-1..8) and the job cost roll-up (FR-JOB-3).

Every figure here is computed from issued documents and recorded work. Void
invoices are excluded; credit notes subtract.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Q, Sum

from trammel.billing.models import Invoice, InvoiceLine, InvoiceTax, Status, WriteOff
from trammel.core.money import round_money

ZERO = Decimal("0")
CATEGORIES = ("materials", "labor", "machine", "other")
ISSUED = (Status.ISSUED, Status.PARTIALLY_PAID, Status.PAID)


def _sign(invoice: Invoice) -> int:
    return -1 if invoice.kind == Invoice.Kind.CREDIT_NOTE else 1


# ---------------------------------------------------------------------------
# Job costing (FR-JOB-3, FR-RPT-1)
# ---------------------------------------------------------------------------


@dataclass
class JobCosting:
    quoted: dict = field(default_factory=lambda: dict.fromkeys(CATEGORIES, ZERO))
    quoted_cost: Decimal = ZERO
    actual: dict = field(default_factory=lambda: dict.fromkeys(CATEGORIES, ZERO))
    hours: dict = field(default_factory=lambda: {"labor": ZERO, "machine": ZERO})
    invoiced: Decimal = ZERO
    paid: Decimal = ZERO

    @property
    def quoted_total(self) -> Decimal:
        return sum(self.quoted.values(), ZERO)

    @property
    def actual_total(self) -> Decimal:
        return sum(self.actual.values(), ZERO)

    @property
    def margin(self) -> Decimal:
        return self.invoiced - self.actual_total

    @property
    def margin_pct(self) -> Decimal | None:
        return round_money(self.margin / self.invoiced * 100) if self.invoiced else None

    def rows(self):
        labels = {
            "materials": "Materials",
            "labor": "Labor",
            "machine": "Machine time",
            "other": "Other",
        }
        return [(labels[c], self.quoted[c], self.actual[c]) for c in CATEGORIES]


def job_costing(job) -> JobCosting:
    from trammel.quotes.models import QuoteRevision
    from trammel.timetracking.models import TimeEntry
    from trammel.workorders.models import MaterialUsage

    c = JobCosting()
    for rev in QuoteRevision.objects.filter(quote__job=job, status="approved"):
        c.quoted_cost += rev.cost_total
        factor = 1 - rev.discount_pct / 100
        for line in rev.lines.all():
            for category, amount in (line.split or {"other": str(line.line_total)}).items():
                key = category if category in CATEGORIES else "other"
                c.quoted[key] += round_money(Decimal(amount) * factor)
        c.quoted["other"] += rev.minimum_adjustment
    for kind, total, minutes in (
        TimeEntry.objects.filter(work_order__job=job)
        .values_list("kind")
        .annotate(Sum("cost"), Sum("minutes"))
    ):
        c.actual["machine" if kind == "machine" else "labor"] += total or ZERO
        c.hours["machine" if kind == "machine" else "labor"] += (minutes or ZERO) / 60
    c.actual["materials"] = (
        MaterialUsage.objects.filter(work_order__job=job).aggregate(s=Sum("total_cost"))["s"]
        or ZERO
    )
    # A final invoice carries a negative line for each deposit applied to it, so
    # deposit invoices plus everything else is the full amount billed — counted once.
    for inv in Invoice.objects.filter(job=job, status__in=ISSUED):
        c.invoiced += _sign(inv) * inv.subtotal
    c.paid = (
        Invoice.objects.filter(job=job, status__in=ISSUED)
        .exclude(kind=Invoice.Kind.CREDIT_NOTE)
        .aggregate(s=Sum("paid_total"))["s"]
        or ZERO
    )
    return c


def profitability(start: dt.date, end: dt.date):
    """Jobs completed or closed in the period, quoted vs. actual (FR-RPT-1)."""
    from trammel.core.models import StateChange
    from trammel.jobs.models import Job

    ids = StateChange.objects.filter(
        entity_type="jobs.job",
        to_state__in=["completed", "closed"],
        at__date__gte=start,
        at__date__lte=end,
    ).values_list("entity_id", flat=True)
    rows = []
    # Internal shop work earns nothing, so it has no margin to report.
    jobs = Job.objects.filter(pk__in=set(ids)).exclude(customer__is_internal=True)
    for job in jobs.select_related("customer"):
        c = job_costing(job)
        rows.append(
            {
                "job": job,
                "quoted": c.quoted_total,
                "quoted_cost": c.quoted_cost,
                "actual": c.actual_total,
                "invoiced": c.invoiced,
                "margin": c.margin,
                "margin_pct": c.margin_pct,
            }
        )
    rows.sort(key=lambda r: r["job"].number)
    totals = {
        k: sum((r[k] for r in rows), ZERO) for k in ("quoted", "actual", "invoiced", "margin")
    }
    return rows, totals


# ---------------------------------------------------------------------------
# Sales (FR-RPT-2)
# ---------------------------------------------------------------------------


def sales(start: dt.date, end: dt.date, group: str = "customer"):
    lines = (
        InvoiceLine.objects.filter(
            invoice__status__in=ISSUED,
            invoice__invoice_date__gte=start,
            invoice__invoice_date__lte=end,
        )
        .exclude(category="deposit")
        .select_related("invoice__customer", "product")
    )
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for line in lines:
        if line.invoice.kind == Invoice.Kind.DEPOSIT:
            continue  # recognized on the final invoice
        amount = _sign(line.invoice) * line.line_total
        if group == "product":
            key = line.product.name if line.product else line.get_category_display()
        elif group == "month":
            key = line.invoice.invoice_date.strftime("%Y-%m")
        else:
            key = line.invoice.customer.name
        totals[key] += amount
    for inv in (
        Invoice.objects.filter(
            status__in=ISSUED, invoice_date__gte=start, invoice_date__lte=end, shipping__gt=0
        )
        .exclude(kind=Invoice.Kind.DEPOSIT)
        .select_related("customer")
    ):
        if group == "product":
            key = "Shipping and delivery"
        elif group == "month":
            key = inv.invoice_date.strftime("%Y-%m")
        else:
            key = inv.customer.name
        totals[key] += _sign(inv) * inv.shipping
    rows = sorted(
        totals.items(), key=lambda kv: (-kv[1], kv[0]) if group != "month" else (kv[0], 0)
    )
    return rows, sum(totals.values(), ZERO)


# ---------------------------------------------------------------------------
# Accounts receivable aging (FR-BIL-5, FR-RPT-3)
# ---------------------------------------------------------------------------

BUCKETS = (
    ("current", "Current"),
    ("d1_30", "1–30 days"),
    ("d31_60", "31–60"),
    ("d61_90", "61–90"),
    ("over_90", "Over 90"),
)


def ar_aging(as_of: dt.date):
    rows: dict = {}
    for inv in (
        Invoice.objects.filter(status__in=(Status.ISSUED, Status.PARTIALLY_PAID))
        .exclude(kind=Invoice.Kind.CREDIT_NOTE)
        .select_related("customer")
    ):
        days = (as_of - inv.due_date).days if inv.due_date else 0
        bucket = (
            "current"
            if days <= 0
            else "d1_30"
            if days <= 30
            else "d31_60"
            if days <= 60
            else "d61_90"
            if days <= 90
            else "over_90"
        )
        row = rows.setdefault(
            inv.customer_id,
            {
                "customer": inv.customer,
                **dict.fromkeys([b for b, _ in BUCKETS], ZERO),
                "total": ZERO,
            },
        )
        row[bucket] += inv.balance
        row["total"] += inv.balance
    ordered = sorted(rows.values(), key=lambda r: -r["total"])
    totals = {b: sum((r[b] for r in ordered), ZERO) for b, _ in BUCKETS}
    totals["total"] = sum((r["total"] for r in ordered), ZERO)
    return ordered, totals


# ---------------------------------------------------------------------------
# Tax (FR-TAX-5, FR-RPT-4)
# ---------------------------------------------------------------------------


def tax_report(start: dt.date, end: dt.date) -> dict:
    """Sales tax by jurisdiction, and gross receipts (B&O) basis by classification."""
    from trammel.tax.models import TaxRate

    invoices = Invoice.objects.filter(
        status__in=ISSUED, invoice_date__gte=start, invoice_date__lte=end
    ).exclude(kind=Invoice.Kind.DEPOSIT)
    by_place: dict = {}
    facilitated: dict[str, Decimal] = defaultdict(lambda: ZERO)
    facilitated_gross: dict[str, Decimal] = defaultdict(lambda: ZERO)
    deposit_lines = dict(
        InvoiceLine.objects.filter(invoice__in=invoices, category="deposit")
        .values_list("invoice")
        .annotate(Sum("line_total"))
    )
    for inv in invoices:
        s = _sign(inv)
        # Deposit lines reduce what is due, not the sale: tax applies to the whole.
        gross = inv.subtotal - deposit_lines.get(inv.pk, ZERO) + inv.shipping
        key = inv.tax_location_code or ("EXEMPT" if inv.tax_method == "exempt" else "NONE")
        row = by_place.setdefault(
            key,
            {
                "code": key,
                "gross": ZERO,
                "taxable": ZERO,
                "exempt": ZERO,
                "tax": ZERO,
                "marketplace": ZERO,
                "invoices": 0,
                "flagged": 0,
            },
        )
        row["gross"] += s * gross
        row["taxable"] += s * inv.taxable_total
        row["exempt"] += s * (gross - inv.taxable_total)
        # Tax a marketplace facilitator collected and pays itself isn't owed.
        row["marketplace" if inv.tax_remitted_by else "tax"] += s * inv.tax_total
        if inv.tax_remitted_by:
            facilitated[inv.tax_remitted_by] += s * (gross + inv.tax_total)
            facilitated_gross[inv.tax_remitted_by] += s * gross
        row["invoices"] += 1
        row["flagged"] += int(inv.tax_flagged)
    components = (
        InvoiceTax.objects.filter(invoice__in=invoices, invoice__tax_remitted_by="")
        .values("code", "name")
        .annotate(tax=Sum("amount"), base=Sum("base"))
    )

    classes: dict[str, Decimal] = defaultdict(lambda: ZERO)
    per_invoice: dict = defaultdict(lambda: defaultdict(lambda: ZERO))
    for line in (
        InvoiceLine.objects.filter(invoice__in=invoices)
        .exclude(category="deposit")
        .select_related("invoice")
    ):
        name = line.gross_receipts_classification or "Unclassified"
        amount = _sign(line.invoice) * line.line_total
        classes[name] += amount
        per_invoice[line.invoice_id][name] += amount
    # A shipping or delivery charge is part of the sale: it follows the
    # classification of the items shipped, in proportion to their amounts.
    for inv in invoices:
        if not inv.shipping:
            continue
        shipping = _sign(inv) * inv.shipping
        shares = per_invoice.get(inv.pk) or {}
        base = sum(shares.values(), ZERO)
        if not base:
            classes["Unclassified"] += shipping
            continue
        for name, amount in shares.items():
            classes[name] += round_money(shipping * amount / base)
    write_offs = WriteOff.objects.filter(written_off_on__gte=start, written_off_on__lte=end)
    bad_debt = write_offs.aggregate(s=Sum("amount"))["s"] or ZERO
    gross_rates = TaxRate.objects.filter(kind=TaxRate.Kind.GROSS_RECEIPTS).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gte=end), effective_from__lte=end
    )
    bo = []
    for name, basis in sorted(classes.items()):
        rate = gross_rates.filter(classification__iexact=name).first()
        bo.append(
            {
                "classification": name,
                "basis": basis,
                "rate": rate.rate if rate else None,
                "tax": round_money(basis * rate.rate / 100) if rate else None,
            }
        )
    rows = sorted(by_place.values(), key=lambda r: r["code"])
    totals = {
        k: sum((r[k] for r in rows), ZERO)
        for k in ("gross", "taxable", "exempt", "tax", "marketplace")
    }
    return {
        "rows": rows,
        "totals": totals,
        "components": list(components),
        "bo": bo,
        "bad_debt": bad_debt,
        # Sales a marketplace facilitator collected the tax on, by marketplace:
        # the gross (before tax) and what the buyer paid in all.
        "marketplaces": [
            {"name": name, "gross": facilitated_gross[name], "paid": facilitated[name]}
            for name in sorted(facilitated)
        ],
        "write_offs": write_offs.select_related("invoice"),
    }


# ---------------------------------------------------------------------------
# Time (FR-RPT-5)
# ---------------------------------------------------------------------------


#: Hours a machine could run on a working day, for the utilization figure.
def workday_hours() -> Decimal:
    """Hours in the shop's working day."""
    from trammel.core.workhours import Hours

    return (Decimal(Hours.load().day_minutes) / 60).quantize(Decimal("0.01")).normalize()


def time_report(start: dt.date, end: dt.date, group: str = "user"):
    from trammel.timetracking.models import TimeEntry

    qs = TimeEntry.objects.filter(
        started_at__date__gte=start, started_at__date__lte=end, ended_at__isnull=False
    )
    if group == "machine":
        qs = qs.filter(kind="machine").values("machine__name")
        key = "machine__name"
    elif group == "job":
        qs = qs.values("work_order__job__number", "work_order__job__title")
        key = "work_order__job__number"
    else:
        qs = qs.filter(kind="labor").values("user__full_name")
        key = "user__full_name"
    rows = qs.annotate(minutes=Sum("minutes"), cost=Sum("cost")).order_by(key)
    from trammel.core.workhours import Hours

    hours_of = Hours.load()
    workdays = sum(
        1
        for i in range((end - start).days + 1)
        if hours_of.is_working_day(start + dt.timedelta(days=i))
    )
    available = workdays * workday_hours()
    out = []
    for r in rows:
        hours = (r["minutes"] or ZERO) / 60
        out.append(
            {
                "name": r[key] or "—",
                "title": r.get("work_order__job__title", ""),
                "hours": hours.quantize(Decimal("0.01")),
                "cost": r["cost"] or ZERO,
                "utilization": round_money(hours / available * 100)
                if group == "machine" and available
                else None,
            }
        )
    return out, available


# ---------------------------------------------------------------------------
# Force-closed jobs and write-offs (FR-RPT-8)
# ---------------------------------------------------------------------------


def force_closed():
    from trammel.core.models import StateChange
    from trammel.jobs.models import Job

    rows = []
    for change in (
        StateChange.objects.filter(entity_type="jobs.job", kind="force")
        .select_related("user")
        .order_by("-at")
    ):
        job = Job.all_objects.filter(pk=change.entity_id).select_related("customer").first()
        rows.append(
            {
                "change": change,
                "job": job,
                "outstanding": change.details.get("outstanding", []),
                "failing": change.details.get("checks_failing", []),
            }
        )
    write_offs = WriteOff.objects.select_related("invoice__customer", "recorded_by")
    return rows, write_offs


def material_use(start: dt.date, end: dt.date, group: str = "material"):
    """Material used on work orders in a period (FR-STK-8), with where the cost came
    from: lots in stock, the price list, or a cost typed in."""
    from django.db.models import Sum

    from trammel.workorders.models import MaterialUsage

    qs = MaterialUsage.objects.filter(created_at__date__range=(start, end))
    if group == "job":
        keys = ["work_order__job__number", "work_order__job__title"]
    elif group == "work_order":
        keys = ["work_order__number", "work_order__title"]
    else:
        keys = ["description", "unit"]
    rows: dict = {}
    for row in qs.values(*keys, "source").annotate(q=Sum("quantity"), cost=Sum("total_cost")):
        key = tuple(row[k] for k in keys)
        entry = rows.setdefault(
            key,
            {
                "name": key[0],
                "detail": key[1],
                "quantity": ZERO,
                "cost": ZERO,
                "stock": ZERO,
                "price_list": ZERO,
                "manual": ZERO,
            },
        )
        entry["quantity"] += row["q"] or ZERO
        entry["cost"] += row["cost"] or ZERO
        entry[row["source"]] += row["cost"] or ZERO
    out = sorted(rows.values(), key=lambda r: (-r["cost"], str(r["name"])))
    totals = {k: sum((r[k] for r in out), ZERO) for k in ("cost", "stock", "price_list", "manual")}
    return out, totals
