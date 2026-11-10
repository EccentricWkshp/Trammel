"""How purchasing plugs into stock, work orders, materials and the home screen."""

from __future__ import annotations

from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from trammel.core import access, agenda, dashboard, panels, report_cards
from trammel.stock import hooks as stock_hooks

from . import services
from .models import POLine, PurchaseOrder, Status, VendorItem


@stock_hooks.on_order
def on_order(item_ids):
    return services.on_order(item_ids)


stock_hooks.restock_action("Create purchase orders", "purchasing:from_restock")


@panels.register("workorders.workorder")
def work_order_purchases(request, wo):
    """What has been bought for this work order, and whether it has arrived (FR-PUR-4)."""
    if not access.has_perm(request.actor, "purchasing.view"):
        return None
    lines = list(
        POLine.objects.filter(work_order=wo, order__deleted_at=None)
        .exclude(order__status=Status.CANCELLED)
        .select_related("order__vendor")
        .order_by("order__created_at", "position")
    )
    if not lines:
        return None
    for line in lines:
        line.state = services.line_status(line)
    waiting = [x for x in lines if x.state != "all"]
    return panels.Panel(
        "Purchases",
        "purchasing/_work_order_panel.html",
        {"lines": lines, "waiting": waiting},
        order=40,
        tab="purchases",
        count=len(waiting) or None,
    )


@panels.register("shop.material")
def material_vendors(request, material):
    """Who the material is bought from, their SKU and the last price."""
    if not access.has_perm(request.actor, "purchasing.view"):
        return None
    items = list(VendorItem.objects.filter(material=material).select_related("vendor"))
    open_lines = list(
        POLine.objects.filter(
            material=material, order__status=Status.ORDERED, order__deleted_at=None
        ).select_related("order__vendor")
    )
    if not items and not open_lines:
        return None
    for line in open_lines:
        line.state = services.line_status(line)
    return panels.Panel(
        "Purchasing",
        "purchasing/_material_panel.html",
        {
            "items": items,
            "open_lines": open_lines,
            "can_costs": access.has_perm(request.actor, "core.see_costs"),
        },
        order=20,
    )


@dashboard.register(
    "purchasing.expected",
    "Deliveries expected",
    order=48,
    permission="purchasing.view",
    description="Purchase orders placed and not yet received, the late ones first.",
)
def expected(request):
    qs = (
        PurchaseOrder.objects.filter(status=Status.ORDERED)
        .select_related("vendor")
        .order_by(F("expected_on").asc(nulls_last=True), "ordered_on")
    )
    rows = list(qs[: dashboard.LIMIT])
    today = timezone.localdate()
    for po in rows:
        po.late = po.expected_on is not None and po.expected_on < today
        po.state = services.receipt_state(po)
    return dashboard.Section(
        template="purchasing/_home_expected.html",
        rows=rows,
        total=qs.count() if len(rows) == dashboard.LIMIT else None,
        more_url=reverse("purchasing:list"),
        empty="Nothing on order.",
    )


@agenda.register(permission="purchasing.view")
def deliveries(request, start, end):
    """Placed orders not yet all here, on the day they are expected."""
    today = timezone.localdate()
    out = []
    for po in PurchaseOrder.objects.filter(
        status=Status.ORDERED, expected_on__range=(start, end)
    ).select_related("vendor"):
        if services.receipt_state(po) == "all":
            continue
        out.append(
            agenda.Entry(
                day=po.expected_on,
                # Not “Delivery”: that is a job's delivery date.
                label="Arriving",
                text=f"{po.number} {po.vendor}",
                url=po.get_absolute_url(),
                late=po.expected_on < today,
                kind="delivery",
            )
        )
    return out


for title, description, url_name, order in (
    (
        "Purchasing spend",
        "Orders placed, by vendor, month or material: goods, tax and shipping.",
        "purchasing:spend",
        20,
    ),
    (
        "Open orders",
        "What is on order and still to come, the late deliveries first.",
        "purchasing:open_orders",
        21,
    ),
    (
        "Price history",
        "What each item cost, from which vendor, order by order.",
        "purchasing:prices",
        22,
    ),
):
    report_cards.register(title, description, url_name, permission="purchasing.view", order=order)
