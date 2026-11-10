"""Stock on the shop floor (FR-STK-7, -8): what a scanned label opens.

Phone-sized pages in the shop-floor layout. A lot's page uses it on a work
order, moves it or scraps it; a location's page counts what is there; a
material's page uses it oldest first. Writes go through the same services as
the office pages.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from trammel.core import access, units
from trammel.core.web import permission_required
from trammel.shop.models import Material
from trammel.workorders import services as wo_services
from trammel.workorders.models import ACTIVE, WorkOrder

from . import services
from .models import Location, StockItem, StockLot

#: The shop-floor work order page remembers the last work order opened here,
#: so “Use on work order” offers it first.
LAST_WORK_ORDER = "floor_work_order"

#: Units a remnant's size is given in.
LENGTH_UNITS = ("in", "ft", "mm", "cm", "m")


def _work_orders(request):
    return (
        access.scoped(request.actor, "workorders.edit", WorkOrder.objects.filter(status__in=ACTIVE))
        .select_related("job")
        .order_by("-updated_at")[:50]
    )


def _use_context(request, material: Material) -> dict:
    can_use = access.has_perm(request.actor, "workorders.edit")
    return {
        "can_use": can_use,
        "work_orders": _work_orders(request) if can_use else [],
        "last_work_order": request.session.get(LAST_WORK_ORDER, ""),
        "unit_choices": material.unit_choices(),
        "length_units": LENGTH_UNITS,
    }


def _common(request) -> dict:
    return {
        "can_edit": access.has_perm(request.actor, "stock.edit"),
        "here": request.get_full_path(),
    }


@permission_required("stock.view")
def index(request):
    """Stock on the shop floor: find a material, see what is low, browse locations."""
    from trammel.core import scan

    term = request.GET.get("q", "").strip()
    if term and (url := scan.find(request, term)):
        return redirect(url)  # a lot number or barcode goes straight to it
    items = services.items_with_totals(StockItem.objects.filter(tracked=True)).order_by(
        "material__name"
    )
    if term:
        items = items.filter(Q(material__name__icontains=term) | Q(material__sku__icontains=term))
    items = list(items[:100])
    low = [i for i in items if i.min_quantity is not None and i.on_hand <= i.min_quantity]
    return render(
        request,
        "stock/floor/index.html",
        {
            "q": term,
            "items": items,
            "low": low if not term else [],
            "locations": [] if term else Location.objects.filter(is_active=True),
        },
    )


@permission_required("stock.view")
def lot(request, pk):
    lot = get_object_or_404(
        StockLot.objects.select_related("item__material", "location", "vendor"), pk=pk
    )
    material = lot.item.material
    return render(
        request,
        "stock/floor/lot.html",
        {
            "lot": lot,
            "material": material,
            "locations": Location.objects.filter(is_active=True).exclude(pk=lot.location_id),
            **_use_context(request, material),
            **_common(request),
        },
    )


@permission_required("stock.view")
def location(request, pk):
    location = get_object_or_404(Location, pk=pk)
    lots = list(
        StockLot.objects.filter(location=location, qty_on_hand__gt=0, item__tracked=True)
        .select_related("item__material")
        .order_by("item__material__name", "received_on")
    )
    by_material: dict = {}
    pieces = []
    for x in lots:
        if x.is_remnant:
            pieces.append(x)
            continue
        entry = by_material.setdefault(x.item.material_id, [x.item.material, Decimal("0"), []])
        entry[1] += x.qty_on_hand
        entry[2].append(x)
    return render(
        request,
        "stock/floor/location.html",
        {
            "location": location,
            "materials": list(by_material.values()),
            "remnants": pieces,
            "children": location.children.filter(is_active=True),
            **_common(request),
        },
    )


@permission_required("shop.view")
def material(request, pk):
    material = access.get_or_404(
        request.actor,
        "shop.view",
        Material.objects.prefetch_related("conversions"),
        pk=pk,
    )
    item = services.item_for(material)
    lots = (
        list(item.lots.filter(qty_on_hand__gt=0).select_related("location"))
        if item and item.tracked and access.has_perm(request.actor, "stock.view")
        else []
    )
    lots.sort(key=lambda x: (not x.is_remnant, x.qty_on_hand, x.received_on))
    return render(
        request,
        "stock/floor/material.html",
        {
            "material": material,
            "tracked": bool(item and item.tracked),
            "lots": lots,
            "on_hand": sum((x.qty_on_hand for x in lots), Decimal("0")),
            **_use_context(request, material),
            **_common(request),
        },
    )


def _back(request, default: str) -> str:
    target = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return default


@permission_required("workorders.edit")
@require_POST
def use(request):
    """Use a material (or one lot of it) on a work order."""
    material = access.get_or_404(
        request.actor, "shop.view", Material.objects.all(), pk=request.POST.get("material")
    )
    back = _back(request, material.get_absolute_url())
    wo = WorkOrder.objects.filter(pk=request.POST.get("work_order") or None).first()
    if wo is None or not access.has_perm(request.actor, "workorders.edit", wo):
        messages.error(request, "Choose the work order it is used on.")
        return redirect(back)
    try:
        quantity = Decimal(request.POST.get("quantity", "").strip())
    except InvalidOperation:
        messages.error(request, "Enter how much was used.")
        return redirect(back)
    lot_id = request.POST.get("lot") or None
    leftover = None
    if request.POST.get("leftover_length", "").strip():
        leftover = (
            request.POST["leftover_length"].strip(),
            request.POST.get("leftover_width", "").strip() or None,
            request.POST.get("leftover_unit") or "in",
        )
    try:
        usages = wo_services.record_material(
            wo,
            material=material,
            quantity=quantity,
            unit=request.POST.get("unit", ""),
            billable=request.POST.get("billable") == "on",
            lot=lot_id,
            leftover=leftover,
            user=request.user,
        )
    except (ValidationError, units.IncompatibleUnits) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        return redirect(back)
    request.session[LAST_WORK_ORDER] = str(wo.pk)
    unit = usages[0].unit
    messages.success(request, f"{services.fmt(quantity, unit)} of {material} used on {wo.number}.")
    return redirect(back)
