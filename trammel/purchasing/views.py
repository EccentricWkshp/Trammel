from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.http import content_disposition_header, urlencode
from django.views.decorators.http import require_POST

from trammel.core import access, sorting, templating
from trammel.core.lifecycle import TransitionError
from trammel.core.services import delete_audited
from trammel.core.web import (
    document_response,
    document_view,
    paginate,
    permission_required,
)
from trammel.shop.models import Vendor
from trammel.stock import services as stock
from trammel.stock.models import Location, StockItem

from . import services
from .forms import LineForm, OrderForm
from .models import POLine, PurchaseOrder, PurchaseReceipt, Status


def _po(request, perm, pk) -> PurchaseOrder:
    return access.get_or_404(
        request.actor, perm, PurchaseOrder.objects.select_related("vendor"), pk=pk
    )


def _errors(request, exc) -> None:
    messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))


SHOW = (
    ("open", "Draft and ordered"),
    ("draft", "Drafts"),
    ("ordered", "Ordered"),
    ("closed", "Closed"),
    ("cancelled", "Canceled"),
    ("all", "All"),
)


SORTS = sorting.Sorts(
    {
        "created": sorting.By("Date created", "created_at", newest_first=True),
        "number": sorting.By("Order number", "number"),
        "vendor": sorting.By("Vendor", "vendor__name"),
        "status": sorting.By("Status", sorting.in_order("status", Status.values)),
        "ordered": sorting.By("Ordered", "ordered_on", newest_first=True),
        "expected": sorting.By("Expected", "expected_on"),
    },
    default="created",
)


@permission_required("purchasing.view")
def order_list(request):
    qs = PurchaseOrder.objects.select_related("vendor").prefetch_related("lines")
    show = request.GET.get("show", "open")
    if show == "open":
        qs = qs.filter(status__in=(Status.DRAFT, Status.ORDERED))
    elif show in Status.values:
        qs = qs.filter(status=show)
    vendor = Vendor.objects.filter(pk=request.GET.get("vendor") or None).first()
    if vendor:
        qs = qs.filter(vendor=vendor)
    term = request.GET.get("q", "").strip()
    if term:
        qs = qs.filter(
            Q(number__icontains=term)
            | Q(vendor_reference__icontains=term)
            | Q(vendor__name__icontains=term)
            | Q(lines__description__icontains=term)
        ).distinct()
    qs, sort = SORTS.apply(request, qs)
    page = paginate(request, qs)
    for po in page:
        po.total = services.totals(po)["total"]
        po.state = services.receipt_state(po) if po.status == Status.ORDERED else ""
    return render(
        request,
        "purchasing/list.html",
        {
            "page": page,
            "show": show,
            "shows": SHOW,
            "vendor": vendor,
            "vendors": Vendor.objects.filter(purchase_orders__isnull=False).distinct(),
            "q": term,
            "query": urlencode({k: v for k, v in request.GET.items() if k != "page" and v}),
            "can_create": access.has_perm(request.actor, "purchasing.create"),
            **sort,
        },
    )


@permission_required("purchasing.create")
def order_new(request):
    initial = {"vendor": request.GET.get("vendor")} if request.GET.get("vendor") else None
    form = OrderForm(request.POST or None, instance=PurchaseOrder(), initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        po = services.create_order(
            d["vendor"],
            expected_on=d["expected_on"],
            vendor_reference=d["vendor_reference"],
            tax_exempt=d["tax_exempt"],
            tax=d["tax"],
            shipping=d["shipping"],
            discount=d["discount"],
            notes=d["notes"],
        )
        return redirect(po)
    return render(request, "purchasing/form.html", {"form": form, "title": "New purchase order"})


@permission_required("purchasing.create")
def order_edit(request, pk):
    po = _po(request, "purchasing.create", pk)
    if po.status in (Status.CLOSED, Status.CANCELLED):
        messages.error(request, f"{po} is {po.get_status_display().lower()}.")
        return redirect(po)
    form = OrderForm(request.POST or None, instance=po)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        fields = {
            k: d[k]
            for k in (
                "expected_on",
                "vendor_reference",
                "tax_exempt",
                "tax",
                "shipping",
                "discount",
                "notes",
            )
            if not form.fields[k].disabled
        }
        try:
            services.update_order(po, **fields)
        except ValidationError as exc:
            _errors(request, exc)
        else:
            messages.success(request, "Saved.")
            return redirect(po)
    return render(request, "purchasing/form.html", {"form": form, "po": po, "title": f"Edit {po}"})


@permission_required("purchasing.view")
def detail(request, pk):
    po = _po(request, "purchasing.view", pk)
    actor = request.actor
    lines = list(po.lines.select_related("material", "work_order"))
    landed = services.landed(po)
    for line in lines:
        line.landed = landed[line.pk]
        line.got, _ = services.received(line)
        line.left = line.quantity - line.got
        line.tracked = line.material_id and stock.tracked_item(line.material) is not None
        if line.tracked:
            item = StockItem.objects.filter(material_id=line.material_id).first()
            line.default_location = item.default_location_id if item else None
    state = services.receipt_state(po)
    can_return = po.status in (Status.ORDERED, Status.CLOSED) and access.has_perm(
        actor, "purchasing.receive"
    )
    for line in lines:
        line.can_send_back = can_return and line.got - services.returned(line) > 0
        line.lots = services.returnable_lots(line) if line.can_send_back else []
    editable = po.editable and access.has_perm(actor, "purchasing.create")
    fixed_note = ""
    if po.status == Status.ORDERED and not po.editable:
        fixed_note = (
            "Lines and charges are fixed now that a delivery has arrived. To change one, "
            "undo the deliveries first."
        )
    can_receive = po.status == Status.ORDERED and access.has_perm(actor, "purchasing.receive")
    receipts = list(
        po.receipts.select_related("line", "lot", "user", "reverses").order_by("created_at")
    )
    undone = {r.reverses_id for r in receipts if r.reverses_id}
    return render(
        request,
        "purchasing/detail.html",
        {
            "po": po,
            "lines": lines,
            "totals": services.totals(po),
            "state": state,
            "receipts": receipts,
            "undone": undone,
            "editable": editable,
            "fixed_note": fixed_note,
            "any_returnable": any(line.can_send_back for line in lines),
            "returns": po.returns.select_related("line", "lot", "user"),
            "adjustments": po.adjustments.select_related("user"),
            "can_adjust": state == "all"
            and po.status in (Status.ORDERED, Status.CLOSED)
            and access.has_perm(actor, "purchasing.receive"),
            # Empty cells after the amount column in the totals rows.
            "trailing": 1 + (po.status != Status.DRAFT) + editable,
            "can_receive": can_receive,
            "can_order": access.has_perm(actor, "purchasing.order"),
            "actions": PurchaseOrder.lifecycle.available(po, actor),
            "locations": Location.objects.filter(is_active=True) if can_receive else [],
            "today": timezone.localdate(),
            "certificate": _resale_certificate(po),
        },
    )


def _resale_certificate(po):
    """The shop's resale certificate file, for an exempt order's vendor."""
    if not po.tax_exempt:
        return None
    from trammel.files.models import ShopDocument
    from trammel.files.services import shop_document

    return shop_document(ShopDocument.RESALE_CERTIFICATE)


@permission_required("purchasing.create")
def line_edit(request, pk, line_pk=None):
    po = _po(request, "purchasing.create", pk)
    if not po.editable:
        messages.error(request, f"{po}'s lines can no longer change.")
        return redirect(po)
    line = get_object_or_404(POLine, pk=line_pk, order=po) if line_pk else POLine(order=po)
    prefix = "line"
    data = request.POST or None
    form = LineForm(data, instance=line, prefix=prefix, vendor=po.vendor)
    if request.method == "POST":
        if "delete" in request.POST and line_pk:
            try:
                services.delete_line(line)
            except ValidationError as exc:
                _errors(request, exc)
            else:
                messages.success(request, f"Removed {line.description}.")
            return redirect(po)
        if form.is_valid():
            line = form.save(commit=False)
            line.order = po
            try:
                services.save_line(line)
            except ValidationError as exc:
                for field, errs in getattr(exc, "message_dict", {None: exc.messages}).items():
                    form.add_error(field if field in form.fields else None, errs)
            else:
                return redirect(po)
    return render(
        request,
        "purchasing/line_form.html",
        {"form": form, "po": po, "line": line, "editing": bool(line_pk)},
    )


@permission_required("purchasing.order")
@require_POST
def place(request, pk):
    po = _po(request, "purchasing.order", pk)
    try:
        services.place(po, actor=request.actor)
    except (ValidationError, TransitionError) as exc:
        _errors(request, exc)
    else:
        po.refresh_from_db()
        messages.success(request, f"Ordered as {po.number}. Print it or send it to the vendor.")
    return redirect(po)


@permission_required("purchasing.view")
@require_POST
def transition(request, pk):
    po = _po(request, "purchasing.view", pk)
    try:
        services.transition(
            po,
            request.POST.get("target", ""),
            actor=request.actor,
            reason=request.POST.get("reason", ""),
        )
    except (ValidationError, TransitionError) as exc:
        _errors(request, exc)
    return redirect(po)


@permission_required("purchasing.create")
@require_POST
def delete(request, pk):
    po = _po(request, "purchasing.create", pk)
    if po.status != Status.DRAFT:
        messages.error(request, "Only a draft can be deleted; cancel an order instead.")
        return redirect(po)
    delete_audited(po)
    messages.success(request, "Draft deleted.")
    return redirect("purchasing:list")


@permission_required("purchasing.receive")
@require_POST
def receive(request, pk):
    """Receive a delivery: a quantity for each line that arrived."""
    po = _po(request, "purchasing.receive", pk)
    try:
        received_on = dt.date.fromisoformat(request.POST.get("received_on", ""))
    except ValueError:
        received_on = None
    count = 0
    try:
        with transaction.atomic():
            for line in po.lines.all():
                raw = request.POST.get(f"qty-{line.pk}", "").strip()
                if not raw:
                    continue
                try:
                    qty = Decimal(raw)
                except InvalidOperation:
                    raise ValidationError(f"“{raw}” is not a quantity.") from None
                if not qty:
                    continue
                location = Location.objects.filter(
                    pk=request.POST.get(f"loc-{line.pk}") or None
                ).first()
                services.receive(
                    line,
                    quantity=qty,
                    location=location,
                    received_on=received_on,
                    user=request.user,
                )
                count += 1
    except ValidationError as exc:
        _errors(request, exc)
        return redirect(po)
    if count:
        messages.success(request, f"Received {count} line{'s' if count != 1 else ''}.")
    else:
        messages.error(request, "Enter how much of each line arrived.")
    return redirect(po)


@permission_required("purchasing.receive")
@require_POST
def unreceive(request, pk, receipt_pk):
    po = _po(request, "purchasing.receive", pk)
    receipt = get_object_or_404(PurchaseReceipt, pk=receipt_pk, order=po)
    try:
        services.unreceive(receipt, user=request.user, reason=request.POST.get("reason", ""))
    except ValidationError as exc:
        _errors(request, exc)
    else:
        messages.success(request, "Receipt undone.")
    return redirect(po)


def _decimal(value: str, what: str) -> Decimal:
    try:
        return Decimal((value or "").strip())
    except InvalidOperation:
        raise ValidationError(f"Enter the {what} as a number.") from None


@permission_required("purchasing.receive")
@require_POST
def send_back(request, pk, line_pk):
    """Return part of a delivery to the vendor (FR-PUR-5)."""
    po = _po(request, "purchasing.receive", pk)
    line = get_object_or_404(POLine, pk=line_pk, order=po)
    from trammel.stock.models import StockLot

    lot = StockLot.objects.filter(pk=request.POST.get("lot") or None).first()
    try:
        services.return_to_vendor(
            line,
            quantity=_decimal(request.POST.get("quantity"), "quantity"),
            refund=_decimal(request.POST.get("refund") or "0", "refund"),
            reason=request.POST.get("reason", ""),
            lot=lot,
            user=request.user,
        )
    except ValidationError as exc:
        _errors(request, exc)
    else:
        messages.success(request, f"Recorded the return of {line.description}.")
    return redirect(po)


@permission_required("purchasing.receive")
@require_POST
def adjust(request, pk):
    """Change the landed cost after delivery: a late freight bill, a corrected invoice."""
    po = _po(request, "purchasing.receive", pk)
    try:
        services.adjust_landed_cost(
            po,
            amount=_decimal(request.POST.get("amount"), "amount"),
            reason=request.POST.get("reason", ""),
            user=request.user,
        )
    except ValidationError as exc:
        _errors(request, exc)
    else:
        messages.success(request, "Landed cost adjusted.")
    return redirect(po)


@permission_required("purchasing.view")
def variances_report(request):
    start, end = _period(request)
    rows = list(services.variances(start, end))
    total = sum((v.amount for v in rows), Decimal("0"))
    if request.GET.get("format") == "csv":
        return _csv(
            request,
            "purchasing-cost-variances",
            ["date", "order", "vendor", "item", "kind", "amount", "note"],
            (
                (
                    v.created_at.date(),
                    v.order.number,
                    v.order.vendor.name,
                    v.line.description,
                    v.get_kind_display(),
                    v.amount,
                    v.note,
                )
                for v in rows
            ),
        )
    return render(
        request,
        "purchasing/report_variances.html",
        {"rows": rows, "total": total, "start": start, "end": end},
    )


@permission_required("purchasing.view")
@document_view
def document(request, pk):
    """The order as sent to the vendor: printable, or a PDF."""
    po = _po(request, "purchasing.view", pk)
    lines = list(po.lines.select_related("material"))
    html = render_to_string(
        "purchasing/document.html",
        {
            "po": po,
            "lines": lines,
            "totals": services.totals(po),
            "business": templating.business_context(request.business),
        },
        request=request,
    )
    if request.GET.get("format") == "pdf":
        try:
            data = templating.render_pdf(html)
        except templating.PdfUnavailable as exc:
            return HttpResponse(str(exc), status=503, content_type="text/plain")
        response = HttpResponse(data, content_type="application/pdf")
        name = po.number or "draft"
        response["Content-Disposition"] = content_disposition_header(
            True, f"purchase-order-{name}.pdf"
        )
        return response
    return document_response(request, html)


@permission_required("purchasing.create")
def from_restock(request):
    """Draft orders for what is low, one per vendor (FR-PUR-1 from FR-STK-1)."""
    rows = list(stock.restock_list().select_related("material__vendor"))
    ordered = services.on_order([i.pk for i in rows])
    for item in rows:
        item.on_order = ordered.get(item.pk, Decimal("0"))
    if request.method == "POST":
        chosen = set(request.POST.getlist("item"))
        items = [i for i in rows if str(i.pk) in chosen]
        if not items:
            messages.error(request, "Tick what to order.")
            return redirect("purchasing:from_restock")
        orders, skipped = services.orders_for_restock(items, user=request.user)
        if orders:
            messages.success(
                request,
                f"Drafted {len(orders)} purchase order{'s' if len(orders) != 1 else ''}. "
                "Check the quantities and prices, then place them.",
            )
        if skipped:
            names = ", ".join(str(i.material) for i in skipped)
            messages.error(request, f"No vendor set, so not ordered: {names}.")
        if len(orders) == 1:
            return redirect(orders[0])
        return redirect("purchasing:list")
    return render(request, "purchasing/from_restock.html", {"rows": rows})


# ---------------------------------------------------------------------------
# Reports (listed on the Reports page)
# ---------------------------------------------------------------------------

SPEND_GROUPS = (("vendor", "Vendor"), ("month", "Month"), ("material", "Material"))


def _period(request):
    today = timezone.localdate()

    def parse(name, default):
        try:
            return dt.date.fromisoformat(request.GET.get(name, ""))
        except ValueError:
            return default

    start = parse("start", today.replace(month=1, day=1))
    end = parse("end", today)
    return (start, end) if start <= end else (end, start)


def _csv(request, name, header, rows):
    from trammel.core import audit
    from trammel.core.web import csv_response

    audit.record("export", entity_type="purchasing", summary=f"Exported {name}")
    return csv_response(f"{name}.csv", header, rows)


@permission_required("purchasing.view")
def spend_report(request):
    start, end = _period(request)
    group = request.GET.get("group", "vendor")
    if group not in dict(SPEND_GROUPS):
        group = "vendor"
    rows, totals = services.spend(start, end, group)
    if request.GET.get("format") == "csv":
        if group == "material":
            return _csv(
                request,
                "purchasing-spend-by-material",
                ["item", "quantity", "unit", "orders", "goods", "landed"],
                (
                    (r["name"], r["quantity"], r["unit"], r["orders"], r["goods"], r["landed"])
                    for r in rows
                ),
            )
        return _csv(
            request,
            f"purchasing-spend-by-{group}",
            [group, "orders", "goods", "tax", "shipping", "discount", "total"],
            (
                (
                    r["name"],
                    r["orders"],
                    r["goods"],
                    r["tax"],
                    r["shipping"],
                    r["discount"],
                    r["total"],
                )
                for r in rows
            ),
        )
    return render(
        request,
        "purchasing/report_spend.html",
        {
            "rows": rows,
            "totals": totals,
            "start": start,
            "end": end,
            "group": group,
            "groups": SPEND_GROUPS,
        },
    )


@permission_required("purchasing.view")
def open_report(request):
    orders = services.open_orders()
    if request.GET.get("format") == "csv":
        return _csv(
            request,
            "purchasing-open-orders",
            [
                "order",
                "vendor",
                "ordered",
                "expected",
                "days_late",
                "lines_to_come",
                "value_to_come",
            ],
            (
                (
                    po.number,
                    po.vendor.name,
                    po.ordered_on,
                    po.expected_on or "",
                    po.days_late,
                    po.lines_left,
                    po.to_come,
                )
                for po in orders
            ),
        )
    return render(
        request,
        "purchasing/report_open.html",
        {"orders": orders, "value": sum((po.to_come for po in orders), Decimal("0"))},
    )


@permission_required("purchasing.view")
def prices_report(request):
    from trammel.shop.models import Material

    material = Material.objects.filter(pk=request.GET.get("material") or None).first()
    vendor = Vendor.objects.filter(pk=request.GET.get("vendor") or None).first()
    lines = services.price_history(material, vendor)
    if request.GET.get("format") == "csv":
        return _csv(
            request,
            "purchasing-price-history",
            ["item", "vendor", "order", "ordered", "quantity", "unit", "price", "landed_per_unit"],
            (
                (
                    line.description,
                    line.order.vendor.name,
                    line.order.number,
                    line.order.ordered_on,
                    line.quantity,
                    line.unit,
                    line.unit_price,
                    line.landed_unit,
                )
                for line in lines
            ),
        )
    return render(
        request,
        "purchasing/report_prices.html",
        {
            "lines": lines,
            "material": material,
            "vendor": vendor,
            "materials": Material.objects.filter(
                pk__in=POLine.objects.exclude(material=None).values("material")
            ).order_by("name"),
            "vendors": Vendor.objects.filter(purchase_orders__isnull=False).distinct(),
        },
    )
