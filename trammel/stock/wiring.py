"""How stock plugs into materials, work orders, the home screen and search."""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils.http import urlencode

from trammel.core import (
    access,
    archive,
    dashboard,
    modules,
    panels,
    report_cards,
    scan,
    search,
    units,
)
from trammel.shop import hooks as shop_hooks
from trammel.shop.models import Material, MaterialBarcode
from trammel.workorders import hooks as wo_hooks

from . import services
from .floor import LENGTH_UNITS
from .models import Location, StockItem, StockLot, StockMove

# The stock list shows every material, so it is where the materials menu leads, and a
# material is added with its stock details.
shop_hooks.materials_home_is("stock:list", "stock")
shop_hooks.material_form_is("stock:add", "stock")


@scan.record
def open_scanned(request, pk):
    """A label's link opens the lot, location or material on the shop floor."""
    actor = request.actor
    if access.has_perm(actor, "stock.view"):
        if StockLot.objects.filter(pk=pk).exists():
            return reverse("stock:floor_lot", args=[pk])
        if Location.objects.filter(pk=pk).exists():
            return reverse("stock:floor_location", args=[pk])
    if access.has_perm(actor, "shop.view") and Material.objects.filter(pk=pk).exists():
        return reverse("stock:floor_material", args=[pk])
    return None


@scan.text
def find_scanned(request, code):
    """A lot number, or a barcode on the material's packaging."""
    actor = request.actor
    if access.has_perm(actor, "stock.view"):
        lot = StockLot.objects.filter(number__iexact=code).first()
        if lot:
            return reverse("stock:floor_lot", args=[lot.pk])
    if access.has_perm(actor, "shop.view"):
        found = MaterialBarcode.objects.filter(code=code).first()
        if found:
            return reverse("stock:floor_material", args=[found.material_id])
    return None


BARCODE = re.compile(r"[0-9A-Za-z.\-]{4,64}")


@scan.unknown
def offer_new_material(request, code):
    """A barcode nobody knows yet: offer to add the material it belongs to."""
    if BARCODE.fullmatch(code) and access.has_perm(request.actor, "shop.manage_materials"):
        return (
            f"{reverse('stock:add')}?{urlencode({'barcode': code})}",
            f"No material has the barcode {code} yet. Add it here.",
        )
    return None


@wo_hooks.carry_over
def plan_bill_of_materials(wo, line, operation):
    services.plan_bom(wo, line, operation)


@wo_hooks.planned_use
def planned_use_mode():
    from .models import StockSettings

    return StockSettings.load().planned_use


@wo_hooks.operation_done
def record_planned_when_done(operation, user):
    """In automatic mode, an operation marked done records what was planned for it."""
    from trammel.workorders import services as wos

    from .models import StockSettings

    if StockSettings.load().planned_use != StockSettings.PlannedUse.AUTO:
        return
    ready = [p for p in wos.ready_to_record(operation.work_order) if p.operation_id == operation.pk]
    if not ready:
        return
    # If stock is short, the operation is still done: the materials wait on the work
    # order's prompt instead.
    try:
        with transaction.atomic():
            wos.use_planned(operation.work_order, ready, user=user)
    except ValidationError, units.IncompatibleUnits:
        pass


@wo_hooks.material_handler
def draw_tracked_material(
    wo,
    *,
    material,
    quantity,
    unit,
    description,
    unit_cost,
    note,
    billable,
    lot,
    user,
    leftover=None,
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
        if leftover:
            raise ValidationError("A return has no leftover piece.")
        return services.return_to_stock(
            wo, material, quantity=-quantity, unit=unit, note=note, billable=billable, user=user
        )
    return services.consume(
        wo,
        material,
        quantity=quantity,
        unit=unit,
        lot=lot,
        leftover=leftover,
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
    pieces: dict = {}
    for lot in lots:
        if lot.is_remnant:
            pieces.setdefault(lot.item.material_id, []).append(lot.size)
            continue
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
        extra = ""
        if p.material_id in pieces:
            sizes = pieces[p.material_id]
            extra = f"; remnant{'s' if len(sizes) > 1 else ''} {', '.join(sizes[:4])}"
            extra += "…" if len(sizes) > 4 else ""
        if p.material_id not in where:
            notes[p.pk] = f"None in stock{extra}"
            continue
        qty, places, material = where[p.material_id]
        notes[p.pk] = f"{services.fmt(qty, material.unit)} in stock: {', '.join(places)}{extra}"
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
    # Remnants first: an offcut that fits is used before a new sheet is cut.
    lots.sort(key=lambda x: (not x.is_remnant, x.received_on, x.created_at))
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
        context["length_units"] = LENGTH_UNITS
        here = {}
        for x in lots:
            if not x.is_remnant:
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


@search.register("Stock locations")
def locations(actor, term):
    if not (modules.is_enabled("stock") and access.has_perm(actor, "stock.view")):
        return
    qs = search.matching(Location.objects.all(), term, ("name", "path"), ("notes",))
    for loc in qs.order_by("path")[:10]:
        yield search.Hit(
            "Stock location",
            loc.path,
            loc.get_absolute_url(),
            "" if loc.is_active else "archived",
        )


for title, description, url_name, order in (
    (
        "Stock valuation",
        "What stock is on hand and worth, on any date, by material, category or location.",
        "stock:valuation",
        10,
    ),
    (
        "Stock adjustments",
        "Stock found, counted, scrapped or taken back out, and what it was worth.",
        "stock:adjustments",
        11,
    ),
    ("Stock history", "Every change to stock: received, used, moved, counted.", "stock:moves", 12),
    ("Restock list", "What is at or below its restock level.", "stock:restock", 13),
    ("Remnants", "Offcuts kept in stock, their size, age and value.", "stock:remnants", 14),
):
    report_cards.register(title, description, url_name, permission="stock.view", order=order)


@archive.on_delete("shop.material")
def drop_unused_stock_record(material):
    """A material going to the trash takes its stock settings with it. Nothing
    has been stocked (or it couldn't be deleted), so no history is lost."""
    StockItem.objects.filter(material=material).delete()


@panels.register("catalog.product")
def bill_of_materials(request, product):
    """What goes into one of the product (FR-STK-6)."""
    if not access.has_perm(request.actor, "catalog.view"):
        return None
    from trammel.shop.models import Material as M

    lines, cost = services.bom_cost(product)
    can_edit = access.has_perm(request.actor, "catalog.edit")
    editing = request.GET.get("edit", "") if can_edit else ""
    other, costing = services.bom_other_cost(product, cost)
    return panels.Panel(
        "Bill of materials",
        "stock/_bom_panel.html",
        {
            "product": product,
            "lines": lines,
            "cost": cost,
            "other": other,
            "costing": costing,
            "in_step": product.cost == cost + other,
            "editing": next((line for line in lines if str(line.pk) == editing), None),
            "can_edit": can_edit,
            "can_costs": access.has_perm(request.actor, "core.see_costs"),
            "materials": M.objects.filter(is_active=True).order_by("name"),
            "units": [c for c, _ in units.UNITS.items() if units.get(c).dimension != "time"],
        },
        order=10,
        tab="bom",
        count=len(lines) or None,
    )
