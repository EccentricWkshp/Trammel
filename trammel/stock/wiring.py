"""How stock plugs into materials, work orders, the home screen and search."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.urls import reverse

from trammel.core import access, dashboard, modules, panels, search
from trammel.shop import hooks as shop_hooks
from trammel.workorders import hooks as wo_hooks

from . import services
from .models import Location, StockLot, StockMove

# The stock list shows every material, so it is where the materials menu leads, and a
# material is added with its stock details.
shop_hooks.materials_home_is("stock:list", "stock")
shop_hooks.material_form_is("stock:add", "stock")


@wo_hooks.material_handler
def draw_tracked_material(
    wo, *, material, quantity, unit, description, unit_cost, note, billable, lot, user
):
    """Tracked materials come out of stock at lot cost; returns go back to their lots."""
    if services.tracked_item(material) is None:
        return None
    if unit_cost is not None:
        raise ValidationError(
            f"{material} is tracked in stock, so its cost comes from the lots used. "
            "Leave the cost blank."
        )
    if quantity < 0:
        return services.return_to_stock(
            wo, material, quantity=-quantity, unit=unit, note=note, billable=billable, user=user
        )
    return services.consume(
        wo,
        material,
        quantity=quantity,
        unit=unit,
        lot=lot,
        description=description,
        note=note,
        billable=billable,
        user=user,
    )


@wo_hooks.planned_note
def planned_in_stock(planned):
    """“120 g in stock: Rack A / Shelf 1” beside each planned material that is tracked."""
    ids = {p.material_id for p in planned if p.material_id}
    if not ids:
        return {}
    where: dict = {}
    lots = (
        StockLot.objects.filter(item__material_id__in=ids, item__tracked=True, qty_on_hand__gt=0)
        .select_related("item__material", "location")
        .order_by("received_on", "created_at")
    )
    for lot in lots:
        entry = where.setdefault(lot.item.material_id, [services.ZERO, [], lot.item.material])
        entry[0] += lot.qty_on_hand
        if lot.location.path not in entry[1]:
            entry[1].append(lot.location.path)
    tracked = set(
        services.StockItem.objects.filter(material_id__in=ids, tracked=True).values_list(
            "material_id", flat=True
        )
    )
    notes = {}
    for p in planned:
        if p.material_id not in tracked:
            continue
        if p.material_id not in where:
            notes[p.pk] = "None in stock"
            continue
        qty, places, material = where[p.material_id]
        notes[p.pk] = f"{services.fmt(qty, material.unit)} in stock: {', '.join(places)}"
    return notes


@panels.register("shop.material")
def material_stock(request, material):
    if not access.has_perm(request.actor, "stock.view"):
        return None
    from .forms import CountForm, LotActionForm, ReceiveForm, TrackingForm

    item = services.item_for(material)
    can_edit = access.has_perm(request.actor, "stock.edit")
    can_manage = access.has_perm(request.actor, "stock.manage")
    tracked = item is not None and item.tracked
    lots = list(item.lots.filter(qty_on_hand__gt=0).select_related("location")) if item else []
    context = {
        "material": material,
        "item": item,
        "tracked": tracked,
        "lots": lots,
        "on_hand": sum((x.qty_on_hand for x in lots), services.ZERO),
        "value": sum((x.value_on_hand for x in lots), services.ZERO),
        "low": tracked
        and item.min_quantity is not None
        and sum((x.qty_on_hand for x in lots), services.ZERO) <= item.min_quantity,
        "moves": list(
            StockMove.objects.filter(item=item)
            .select_related("lot", "location", "user")
            .order_by("-created_at", "-id")[:25]
        )
        if item
        else [],
        "can_edit": can_edit and tracked,
        "can_manage": can_manage,
        "can_costs": access.has_perm(request.actor, "core.see_costs"),
        "unit_label": material.get_unit_display(),
    }
    if can_manage:
        context["tracking_form"] = TrackingForm(instance=item, material=material)
    if can_edit and tracked:
        context["receive_form"] = ReceiveForm(material=material, item=item)
        context["count_form"] = CountForm(material=material, item=item)
        context["lot_form"] = LotActionForm(material=material)
        context["locations"] = Location.objects.filter(is_active=True)
        here = {}
        for x in lots:
            here[x.location] = here.get(x.location, services.ZERO) + x.qty_on_hand
        context["by_location"] = sorted(here.items(), key=lambda kv: kv[0].path)
    return panels.Panel(
        "Stock",
        "stock/_material_panel.html",
        context,
        order=10,
        tab="stock",
        count=len(lots) or None,
    )


@dashboard.register(
    "stock.low",
    "Low stock",
    order=45,
    permission="stock.view",
    description="Tracked materials at or below their restock level.",
)
def low_stock(request):
    qs = services.restock_list()
    rows = list(qs[: dashboard.LIMIT])
    return dashboard.Section(
        template="stock/_home_low.html",
        rows=rows,
        total=qs.count() if len(rows) == dashboard.LIMIT else None,
        more_url=reverse("stock:restock"),
        empty="Nothing needs restocking.",
    )


@search.register("Stock lots")
def lots(actor, term):
    if not (modules.is_enabled("stock") and access.has_perm(actor, "stock.view")):
        return
    for lot in StockLot.objects.filter(number__icontains=term.strip()).select_related(
        "item__material", "location"
    )[:10]:
        yield search.Hit(
            "Stock lot",
            f"{lot.number} {lot.item.material}",
            lot.get_absolute_url(),
            str(lot.location),
        )
