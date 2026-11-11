"""Stock writes (FR-STK-1..3, -8). Web, API and the work order hook call these.

Rules every function keeps:

* Lock the item's row first, then its lots, so two people drawing the last
  unit cannot both succeed.
* A draw from a lot costs value × taken / on hand, rounded to the cent, and the
  draw that empties a lot takes all of its remaining value. Lot values therefore
  reach exactly zero and the ledger adds up to the cent.
* Every change to a lot is a `StockMove`, written by `_move`, which also keeps
  the lot's running quantity and value.
"""

from __future__ import annotations

import re
import uuid
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Max, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from trammel.catalog import costing
from trammel.core import audit, numbering, units
from trammel.core.money import round_money
from trammel.core.services import save_audited
from trammel.shop.models import Material

from .models import Location, Reason, StockItem, StockLot, StockMove, StockSettings

ZERO = Decimal("0")
Q4 = Decimal("0.0001")
USAGE = "workorders.materialusage"
LABEL_LINK = re.compile(r"/s/([0-9a-fA-F-]{36})/?")


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Q4, rounding=ROUND_HALF_UP)


def fmt(value: Decimal, unit: str) -> str:
    from trammel.core.money import trim_decimal

    return f"{trim_decimal(value)} {units.get(unit).label}"


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


@transaction.atomic
def save_location(location: Location, *, before: dict | None = None) -> Location:
    """Save a location and keep its path, and its children's, up to date."""
    location.name = location.name.strip()
    if not location.name:
        raise ValidationError({"name": "Name the location."})
    if " / " in location.name:
        raise ValidationError({"name": "A location name cannot contain “ / ”."})
    parent = location.parent
    seen = {location.pk}
    while parent is not None:
        if parent.pk in seen:
            raise ValidationError({"parent": "A location cannot be inside itself."})
        seen.add(parent.pk)
        parent = parent.parent
    location.path = _path(location)
    save_audited(location, before=before)
    _repath_children(location)
    return location


def _path(location: Location) -> str:
    return f"{location.parent.path} / {location.name}" if location.parent else location.name


def _repath_children(location: Location) -> None:
    for child in Location.objects.filter(parent=location):
        path = _path(child)
        if child.path != path:
            child.path = path
            child.save(update_fields=["path", "updated_at"])
        _repath_children(child)


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------


def item_for(material: Material) -> StockItem | None:
    return StockItem.objects.filter(material=material).first()


def tracked_item(material: Material) -> StockItem | None:
    return StockItem.objects.filter(material=material, tracked=True).first()


def on_hand(item: StockItem, location: Location | None = None, *, remnants: bool = True) -> Decimal:
    lots = item.lots.all() if location is None else item.lots.filter(location=location)
    if not remnants:
        lots = lots.filter(length__isnull=True)
    return lots.aggregate(q=Coalesce(Sum("qty_on_hand"), ZERO))["q"]


def value_on_hand(item: StockItem) -> Decimal:
    return item.lots.aggregate(v=Coalesce(Sum("value_on_hand"), ZERO))["v"]


@transaction.atomic
def set_tracking(
    material: Material,
    *,
    tracked: bool,
    min_quantity: Decimal | None = None,
    reorder_quantity: Decimal | None = None,
    default_location: Location | None = None,
) -> StockItem:
    item = StockItem.objects.select_for_update().filter(material=material).first()
    if item is None:
        item = StockItem(material=material)
    before = audit.snapshot(item) if item.pk and not item._state.adding else {}
    if not tracked and item.tracked and not item._state.adding:
        left = on_hand(item)
        if left > 0:
            raise ValidationError(
                f"{fmt(left, material.unit)} of {material} is still in stock. "
                "Use, count or scrap it before you stop tracking it."
            )
    item.tracked = tracked
    item.min_quantity = min_quantity
    item.reorder_quantity = reorder_quantity
    item.default_location = default_location
    item.save()
    audit.record(
        "stock.tracking",
        material,
        summary=f"{material}: {'tracked in stock' if tracked else 'not tracked'}",
        before=before or None,
        after=audit.snapshot(item),
    )
    return item


def disable_problems() -> list[str]:
    """The stock module stays on while materials are tracked (ModuleManifest.disable_check)."""
    n = StockItem.objects.filter(tracked=True).count()
    if n:
        return [
            f"{n} material{'s are' if n != 1 else ' is'} tracked in stock. "
            "Stop tracking them before you turn stock off."
        ]
    return []


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def _lock(item: StockItem) -> StockItem:
    return StockItem.objects.select_for_update().select_related("material").get(pk=item.pk)


def _require_tracked(item: StockItem | None, material: Material) -> StockItem:
    if item is None or not item.tracked:
        raise ValidationError(f"{material} is not tracked in stock.")
    return item


def _stock_quantity(material: Material, quantity, unit: str) -> Decimal:
    """`quantity unit` in the stock unit, to four places. Must be more than zero."""
    try:
        q = _q(material.to_stock(quantity, unit or material.unit))
    except units.IncompatibleUnits as exc:
        raise ValidationError(str(exc)) from None
    if q <= 0:
        raise ValidationError("Enter a quantity more than zero.")
    return q


def _whole_remnant(lot: StockLot, quantity: Decimal, doing: str) -> None:
    """A remnant is one piece: it is moved, scrapped or used whole."""
    if lot.is_remnant and quantity != lot.qty_on_hand:
        raise ValidationError(
            f"{lot.number} is a remnant ({lot.size}); {doing} all of it. "
            "To keep part of it, cut that part off as a remnant first."
        )


def _draw_value(lot: StockLot, quantity: Decimal) -> Decimal:
    if quantity == lot.qty_on_hand:
        return lot.value_on_hand
    return round_money(lot.value_on_hand * quantity / lot.qty_on_hand)


def _source(source) -> dict:
    if source is None:
        return {}
    return {"source_type": source._meta.label_lower, "source_id": source.pk}


def _move(
    lot: StockLot,
    reason: str,
    quantity: Decimal,
    value: Decimal,
    *,
    user=None,
    note: str = "",
    entered: tuple[Decimal, str] | None = None,
    source=None,
    source_type: str = "",
    source_id=None,
) -> StockMove:
    lot.qty_on_hand += quantity
    lot.value_on_hand += value
    lot.save(update_fields=["qty_on_hand", "value_on_hand"])
    extra = _source(source) or {"source_type": source_type, "source_id": source_id}
    return StockMove.objects.create(
        item_id=lot.item_id,
        lot=lot,
        location_id=lot.location_id,
        reason=reason,
        quantity=quantity,
        value=value,
        entered_quantity=entered[0] if entered else None,
        entered_unit=entered[1] if entered else "",
        note=note[:300],
        user=user,
        **extra,
    )


def _new_lot(
    item: StockItem,
    *,
    reason: str,
    quantity: Decimal,
    value: Decimal,
    location: Location,
    received_on=None,
    vendor=None,
    parent: StockLot | None = None,
    unit_cost: Decimal | None = None,
    note: str = "",
    user=None,
    entered=None,
    source=None,
    piece: dict | None = None,
) -> StockLot:
    lot = StockLot.objects.create(
        **(piece or {}),
        number=numbering.next_number("stock_lot"),
        item=item,
        location=location,
        received_on=received_on or timezone.localdate(),
        origin=reason,
        parent=parent,
        vendor=vendor,
        unit_cost=unit_cost if unit_cost is not None else _q(value / quantity),
        note=note[:300],
    )
    _move(lot, reason, quantity, value, user=user, note=note, entered=entered, source=source)
    return lot


def current_cost(item: StockItem) -> Decimal:
    """Cost of one stock unit now: the newest lot's, else the material's own."""
    newest = item.lots.filter(unit_cost__gt=0).order_by("-received_on", "-created_at").first()
    return newest.unit_cost if newest else item.material.unit_cost


def _location(item: StockItem, location: Location | None) -> Location:
    location = location or item.default_location
    if location is None:
        raise ValidationError("Say where the stock is.")
    return location


@transaction.atomic
def receive(
    material: Material,
    *,
    quantity: Decimal,
    unit: str = "",
    location: Location | None = None,
    unit_cost: Decimal | None = None,
    total_cost: Decimal | None = None,
    received_on=None,
    vendor=None,
    note: str = "",
    opening: bool = False,
    update_cost: bool = True,
    user=None,
) -> StockLot:
    """Stock coming in: a new lot at its cost. `unit_cost` is per entered unit;
    give it or the `total_cost`. A receipt (not an opening balance) makes its cost
    the material's cost, so quotes use the latest price paid."""
    item = _lock(_require_tracked(item_for(material), material))
    unit = unit or material.unit
    qty = _stock_quantity(material, quantity, unit)
    if total_cost is None:
        if unit_cost is None:
            raise ValidationError("Give the cost, per unit or in total.")
        total_cost = round_money(Decimal(quantity) * Decimal(unit_cost))
    if total_cost < 0:
        raise ValidationError("A cost cannot be negative.")
    lot = _new_lot(
        item,
        reason=Reason.OPENING if opening else Reason.RECEIVE,
        quantity=qty,
        value=round_money(total_cost),
        location=_location(item, location),
        received_on=received_on,
        vendor=vendor,
        note=note,
        user=user,
        entered=(Decimal(quantity), unit) if unit != material.unit else None,
    )
    if update_cost and not opening and lot.unit_cost != material.unit_cost:
        before = audit.snapshot(material)
        Material.objects.filter(pk=material.pk).update(unit_cost=lot.unit_cost)
        material.unit_cost = lot.unit_cost
        refresh_costs_using(material)
        audit.record(
            "material.cost",
            material,
            summary=f"{material}: cost now {lot.unit_cost} from {lot.number}",
            before=before,
            after=audit.snapshot(material),
        )
    audit.record(
        "stock.opening" if opening else "stock.receive",
        material,
        summary=f"{material}: {fmt(qty, material.unit)} into {lot.location} ({lot.number})",
    )
    return lot


@transaction.atomic
def unreceive_lot(lot: StockLot, *, note: str = "", user=None) -> StockMove:
    """Take a whole lot back out: its receipt was a mistake (FR-PUR-3). The caller
    checks that the lot is untouched."""
    _lock(lot.item)
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    if lot.qty_on_hand <= 0:
        raise ValidationError(f"{lot.number} is already empty.")
    return _move(lot, Reason.UNRECEIVE, -lot.qty_on_hand, -lot.value_on_hand, user=user, note=note)


@transaction.atomic
def take_out(
    lot: StockLot, *, quantity: Decimal, reason: str, note: str = "", user=None
) -> StockMove:
    """Remove part of a lot for a reason of the caller's (sent back to the vendor),
    at its share of the lot's value."""
    _lock(lot.item)
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    quantity = _q(quantity)
    if quantity <= 0:
        raise ValidationError("Enter a quantity more than zero.")
    if quantity > lot.qty_on_hand:
        raise ValidationError(
            f"{lot.number} has only {fmt(lot.qty_on_hand, lot.item.material.unit)} left."
        )
    _whole_remnant(lot, quantity, "send back")
    return _move(lot, reason, -quantity, -_draw_value(lot, quantity), user=user, note=note)


@transaction.atomic
def revalue(lot: StockLot, amount: Decimal, *, note: str = "", user=None) -> Decimal:
    """Change what a lot is worth without changing how much there is (a late freight
    bill). Returns the part of `amount` the lot could take: none once it is empty,
    and never below zero value."""
    _lock(lot.item)
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    if lot.qty_on_hand <= 0 or not amount:
        return ZERO
    applied = max(amount, -lot.value_on_hand)
    if applied:
        _move(lot, Reason.REVALUE, ZERO, applied, user=user, note=note)
    return applied


@transaction.atomic
def transfer(
    lot: StockLot, *, quantity: Decimal, unit: str = "", to: Location, user=None, note: str = ""
) -> StockLot:
    """Move some or all of a lot. All of it keeps the lot; part of it splits off
    a new lot that keeps the received date and cost. Returns the lot at `to`."""
    item = _lock(lot.item)
    material = item.material
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    if to.pk == lot.location_id:
        raise ValidationError(f"{lot.number} is already in {to}.")
    qty = _stock_quantity(material, quantity, unit)
    if qty > lot.qty_on_hand:
        raise ValidationError(f"{lot.number} has only {fmt(lot.qty_on_hand, material.unit)}.")
    _whole_remnant(lot, qty, "move")
    value = _draw_value(lot, qty)
    from_location = lot.location
    _move(lot, Reason.TRANSFER, -qty, -value, user=user, note=f"To {to}. {note}".strip())
    if lot.qty_on_hand == 0:
        lot.location = to
        lot.save(update_fields=["location"])
        _move(lot, Reason.TRANSFER, qty, value, user=user, note=f"From {from_location}")
        moved = lot
    else:
        moved = _new_lot(
            item,
            reason=Reason.TRANSFER,
            quantity=qty,
            value=value,
            location=to,
            received_on=lot.received_on,
            vendor=lot.vendor,
            parent=lot,
            unit_cost=lot.unit_cost,
            note=f"From {lot.number} in {from_location}",
            user=user,
        )
    audit.record(
        "stock.transfer",
        material,
        summary=f"{material}: {fmt(qty, material.unit)} of {lot.number} "
        f"from {from_location} to {to}",
    )
    return moved


@transaction.atomic
def scrap(lot: StockLot, *, quantity: Decimal, unit: str = "", note: str, user=None) -> StockMove:
    if not note.strip():
        raise ValidationError("Say why it is scrapped.")
    item = _lock(lot.item)
    material = item.material
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    qty = _stock_quantity(material, quantity, unit)
    if qty > lot.qty_on_hand:
        raise ValidationError(f"{lot.number} has only {fmt(lot.qty_on_hand, material.unit)}.")
    _whole_remnant(lot, qty, "scrap")
    move = _move(lot, Reason.SCRAP, -qty, -_draw_value(lot, qty), user=user, note=note.strip())
    audit.record(
        "stock.scrap",
        material,
        summary=f"{material}: scrapped {fmt(qty, material.unit)} of {lot.number}",
        reason=note.strip(),
    )
    return move


@transaction.atomic
def count(
    material: Material,
    *,
    location: Location,
    counted: Decimal,
    unit: str = "",
    expected: Decimal | None,
    note: str = "",
    user=None,
) -> Decimal:
    """Set what is at `location` to what was counted (FR-STK-2). `expected` is the
    quantity the counter was shown; if stock moved since, the count is refused
    rather than overwriting that change. Returns the difference, in stock units."""
    item = _lock(_require_tracked(item_for(material), material))
    # Remnants are handled one by one (used, moved or scrapped), not counted.
    here = on_hand(item, location, remnants=False)
    if expected is not None and _q(expected) != here:
        raise ValidationError(
            f"Stock of {material} in {location} changed while you were counting "
            f"(now {fmt(here, material.unit)}). Count again."
        )
    unit = unit or material.unit
    if Decimal(counted) < 0:
        raise ValidationError("A count cannot be negative.")
    try:
        found = _q(material.to_stock(counted, unit))
    except units.IncompatibleUnits as exc:
        raise ValidationError(str(exc)) from None
    diff = found - here
    note = note.strip() or "Counted"
    if diff < 0:
        remaining = -diff
        lots = StockLot.objects.select_for_update().filter(
            item=item, location=location, qty_on_hand__gt=0, length__isnull=True
        )
        for lot in lots:
            take = min(lot.qty_on_hand, remaining)
            _move(lot, Reason.COUNT, -take, -_draw_value(lot, take), user=user, note=note)
            remaining -= take
            if not remaining:
                break
    elif diff > 0:
        cost = _cost_or_refuse(item)
        _new_lot(
            item,
            reason=Reason.COUNT,
            quantity=diff,
            value=round_money(diff * cost),
            location=location,
            unit_cost=cost,
            note=note,
            user=user,
        )
    audit.record(
        "stock.count",
        material,
        summary=f"{material} in {location}: counted {fmt(found, material.unit)}"
        + (f" ({'+' if diff > 0 else ''}{fmt(diff, material.unit)})" if diff else ", no change"),
        reason=note if diff else "",
    )
    return diff


def _cost_or_refuse(item: StockItem) -> Decimal:
    cost = current_cost(item)
    if cost <= 0:
        raise ValidationError(
            f"{item.material} has no cost yet. Give it a cost on the material first, "
            "so stock found is not valued at nothing."
        )
    return cost


# ---------------------------------------------------------------------------
# Remnants (FR-STK-5)
# ---------------------------------------------------------------------------

#: The area unit that goes with a length unit, for pieces measured both ways.
AREA_OF = {
    "mm": "sq_mm",
    "cm": "sq_cm",
    "m": "sq_m",
    "in": "sq_in",
    "ft": "sq_ft",
    "linear_ft": "sq_ft",
    "yd": "sq_yd",
}


def _dimension(value, name: str) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        value = Decimal(str(value))
    except ArithmeticError:
        raise ValidationError(f"The {name} is not a number.") from None
    if value <= 0:
        raise ValidationError(f"The {name} must be more than zero.")
    return value


def _piece(material: Material, length, width, unit: str) -> dict:
    """A piece's size as lot fields, plus its `quantity` in the stock unit.

    With a width, the piece is measured by area (sheet goods); without, by
    length (boards and bars). The material's conversions say how much stock
    that is."""
    length = _dimension(length, "length")
    width = _dimension(width, "width")
    if length is None:
        raise ValidationError("Give the remnant's length.")
    if unit not in AREA_OF:
        raise ValidationError("Give the size in mm, cm, m, in, ft or yd.")
    try:
        if width is None:
            quantity = material.to_stock(length, unit)
        else:
            quantity = material.to_stock(length * width, AREA_OF[unit])
    except units.IncompatibleUnits:
        how = "by area" if width is not None else "by length"
        raise ValidationError(
            f"{material} cannot be measured {how}. "
            + (
                "Leave the width out."
                if width is not None
                else "Give its width too, or give it a length conversion on the material."
            )
        ) from None
    quantity = _q(quantity)
    if quantity <= 0:
        raise ValidationError("That piece is too small to keep.")
    long_side, short_side = (length, width) if width is None or length >= width else (width, length)
    return {
        "quantity": quantity,
        "length": length,
        "width": width,
        "dim_unit": unit,
        "long_mm": units.canonical(long_side, unit).quantize(Decimal("0.001")),
        "short_mm": units.canonical(short_side, unit).quantize(Decimal("0.001"))
        if short_side is not None
        else None,
    }


@transaction.atomic
def cut_remnant(
    lot: StockLot,
    *,
    length,
    width=None,
    unit: str,
    location: Location | None = None,
    note: str = "",
    user=None,
) -> StockLot:
    """Record a piece of a lot as a remnant of its own: its size, its share of the
    lot's value, and where it is kept."""
    item = _lock(lot.item)
    material = item.material
    lot = StockLot.objects.select_for_update().get(pk=lot.pk)
    piece = _piece(material, length, width, unit)
    qty = piece.pop("quantity")
    if qty > lot.qty_on_hand:
        raise ValidationError(
            f"{lot.number} has only {fmt(lot.qty_on_hand, material.unit)}; "
            f"a piece that size is {fmt(qty, material.unit)}."
        )
    value = _draw_value(lot, qty)
    _move(lot, Reason.REMNANT, -qty, -value, user=user, note=note or "Remnant cut off")
    remnant = _new_lot(
        item,
        reason=Reason.REMNANT,
        quantity=qty,
        value=value,
        location=location or lot.location,
        received_on=lot.received_on,
        vendor=lot.vendor,
        parent=lot,
        unit_cost=lot.unit_cost,
        note=note or f"Cut from {lot.number}",
        user=user,
        piece=piece,
    )
    audit.record(
        "stock.remnant",
        material,
        summary=f"{material}: remnant {remnant.number} ({remnant.size}) from {lot.number}",
    )
    return remnant


def remnants(material: Material | None = None, *, length=None, width=None, unit: str = "in"):
    """Remnants on hand at least `length` × `width`, either way round, smallest first."""
    qs = StockLot.objects.filter(
        qty_on_hand__gt=0, length__isnull=False, item__tracked=True
    ).select_related("item__material", "location")
    if material is not None:
        qs = qs.filter(item__material=material)
    sides = [d for d in (_dimension(length, "length"), _dimension(width, "width")) if d]
    if sides and unit in AREA_OF:
        need = sorted((units.canonical(d, unit) for d in sides), reverse=True)
        qs = qs.filter(long_mm__gte=need[0])
        if len(need) == 2:
            qs = qs.filter(short_mm__gte=need[1])
    return qs.order_by("item__material__name", "qty_on_hand", "received_on")


# ---------------------------------------------------------------------------
# Work orders
# ---------------------------------------------------------------------------


@transaction.atomic
def consume(
    wo,
    material: Material,
    *,
    quantity: Decimal,
    unit: str = "",
    lot=None,
    leftover: tuple | None = None,
    description: str = "",
    note: str = "",
    billable: bool = True,
    user=None,
) -> list:
    """Use stock on a work order: oldest lots first, or the lot given. Writes one
    usage per lot drawn from, at that lot's cost (FR-STK-3).

    Remnants are used only when picked. `leftover` is (length, width, unit) of a
    piece cut off what was used and kept: it goes back to stock as a remnant of
    the last lot drawn, and the work order pays only for the rest (FR-STK-5)."""
    from trammel.workorders import services as wo_services
    from trammel.workorders.models import MaterialUsage

    item = _lock(_require_tracked(item_for(material), material))
    unit = unit or material.unit
    quantity = Decimal(quantity)
    qty = _stock_quantity(material, quantity, unit)
    lots = StockLot.objects.select_for_update().filter(item=item, qty_on_hand__gt=0)
    if lot is not None:
        lots = lots.filter(pk=find_lot(item, lot).pk)
        if not lots.exists():
            raise ValidationError(f"That lot has no {material} left.")
    else:
        lots = lots.filter(length__isnull=True)
    lots = list(lots.order_by("received_on", "created_at", "id"))
    available = sum((x.qty_on_hand for x in lots), ZERO)
    if qty > available:
        short = qty - available
        if lot is not None:
            raise ValidationError(
                f"{lots[0].number} has only {fmt(available, material.unit)} of {material}."
            )
        if StockSettings.load().shortfall != StockSettings.Shortfall.FOUND:
            pieces = item.lots.filter(qty_on_hand__gt=0, length__isnull=False).count()
            hint = (
                f" There {'is' if pieces == 1 else 'are'} also {pieces} "
                f"remnant{'' if pieces == 1 else 's'}, used only when picked."
                if pieces
                else ""
            )
            raise ValidationError(
                f"Only {fmt(available, material.unit)} of {material} is in stock, and "
                f"{fmt(qty, material.unit)} is needed. Count or receive it first.{hint}"
            )
        cost = _cost_or_refuse(item)
        where = item.default_location or (lots[-1].location if lots else None)
        if where is None:
            raise ValidationError(
                f"{material} has no stock and no default location to record what was found."
            )
        lots.append(
            _new_lot(
                item,
                reason=Reason.ADJUST,
                quantity=short,
                value=round_money(short * cost),
                location=where,
                unit_cost=cost,
                note=f"Found: {wo.number} used more than was in stock",
                user=user,
            )
        )
    description = description or str(material)
    usages, remaining, entered_so_far = [], qty, ZERO
    picks = []
    for x in lots:
        take = min(x.qty_on_hand, remaining)
        picks.append((x, take))
        remaining -= take
        if not remaining:
            break
    if lot is not None and lots[0].is_remnant and qty != lots[0].qty_on_hand:
        raise ValidationError(
            f"{lots[0].number} is a remnant ({lots[0].size}) and is used whole. "
            "Give the size of what is left over, and that is kept as a new remnant."
        )
    piece = kept = None
    if leftover:
        piece = _piece(material, *leftover)
        kept = piece.pop("quantity")
        if kept >= picks[-1][1]:
            raise ValidationError(
                "The leftover piece is as big as what was taken. Use less instead, "
                "or leave the leftover out."
            )
    for n, (x, take) in enumerate(picks):
        value = _draw_value(x, take)
        last = n == len(picks) - 1
        if last:
            entered = quantity - entered_so_far
        else:
            entered = _q(material.convert(take, material.unit, unit))
            entered_so_far += entered
        kept_value = ZERO
        if last and kept:
            # The job pays for what it used; the leftover's share goes back with it.
            kept_value = round_money(value * kept / take)
            entered -= _q(material.convert(kept, material.unit, unit))
        usage = wo_services.write_usage(
            wo,
            material=material,
            description=description,
            quantity=entered,
            unit=unit,
            unit_cost=_q((value - kept_value) / entered) if entered else ZERO,
            total_cost=value - kept_value,
            note=note if len(picks) == 1 else f"{note} ({x.number})".strip(),
            billable=billable,
            source=MaterialUsage.Source.STOCK,
            user=user,
        )
        _move(
            x,
            Reason.CONSUME,
            -take,
            -value,
            user=user,
            entered=(entered, unit) if unit != material.unit else None,
            source=usage,
            note=wo.number,
        )
        if last and kept:
            _new_lot(
                item,
                reason=Reason.REMNANT,
                quantity=kept,
                value=kept_value,
                location=x.location,
                received_on=x.received_on,
                vendor=x.vendor,
                parent=x,
                unit_cost=x.unit_cost,
                note=f"Left over from {wo.number}",
                user=user,
                source=usage,
                piece=piece,
            )
        usages.append(usage)
    return usages


def find_lot(item: StockItem, value) -> StockLot:
    """A lot of `item`, given as a lot, its id, its number, or its label's link."""
    if isinstance(value, StockLot):
        value = value.pk
    text = str(value).strip()
    if match := LABEL_LINK.search(text):
        text = match.group(1)
    try:
        found = item.lots.filter(pk=uuid.UUID(text)).first()
    except ValueError:
        found = item.lots.filter(number__iexact=text).first()
    if found is None:
        raise ValidationError(f"{item.material} has no lot “{text}”.")
    return found


def returnable(wo, item: StockItem) -> list[tuple[StockMove, Decimal, Decimal]]:
    """This work order's draws of `item` not yet returned, newest first:
    (consume move, quantity left to return, value left to return)."""
    from trammel.workorders.models import MaterialUsage

    usage_ids = MaterialUsage.objects.filter(work_order=wo, material=item.material).values("pk")
    draws = StockMove.objects.filter(
        item=item, reason=Reason.CONSUME, source_type=USAGE, source_id__in=usage_ids
    ).order_by("-created_at", "-id")
    back = {
        row["source_id"]: (row["q"], row["v"])
        for row in StockMove.objects.filter(
            item=item,
            reason__in=(Reason.RETURN, Reason.REMNANT),
            source_type=USAGE,
            source_id__in=usage_ids,
        )
        .values("source_id")
        .annotate(q=Sum("quantity"), v=Sum("value"))
    }
    out = []
    for move in draws:
        q_back, v_back = back.get(move.source_id, (ZERO, ZERO))
        q_left = -move.quantity - q_back
        if q_left > 0:
            out.append((move, q_left, -move.value - v_back))
    return out


@transaction.atomic
def return_to_stock(
    wo,
    material: Material,
    *,
    quantity: Decimal,
    unit: str = "",
    note: str = "",
    billable: bool = True,
    user=None,
) -> list:
    """Put back material a work order drew and did not use: into the lots it came
    from, newest draw first, at the cost it left at. Writes negative usages."""
    from trammel.workorders import services as wo_services
    from trammel.workorders.models import MaterialUsage

    item = _lock(_require_tracked(item_for(material), material))
    unit = unit or material.unit
    quantity = Decimal(quantity)
    qty = _stock_quantity(material, quantity, unit)
    open_draws = returnable(wo, item)
    left = sum((q for _, q, _ in open_draws), ZERO)
    if qty > left:
        raise ValidationError(
            f"{wo.number} has used {fmt(left, material.unit)} of {material} from stock; "
            "no more than that can be returned."
        )
    picks, remaining = [], qty
    for move, q_left, v_left in open_draws:
        take = min(q_left, remaining)
        value = v_left if take == q_left else round_money(-move.value * take / -move.quantity)
        picks.append((move, take, value))
        remaining -= take
        if not remaining:
            break
    usages, entered_so_far = [], ZERO
    for n, (move, take, value) in enumerate(picks):
        lot = StockLot.objects.select_for_update().get(pk=move.lot_id)
        if n == len(picks) - 1:
            entered = quantity - entered_so_far
        else:
            entered = _q(material.convert(take, material.unit, unit))
            entered_so_far += entered
        usage = wo_services.write_usage(
            wo,
            material=material,
            description=str(material),
            quantity=-entered,
            unit=unit,
            unit_cost=_q(value / entered),
            total_cost=-value,
            note=(note or "Returned to stock").strip(),
            billable=billable,
            source=MaterialUsage.Source.STOCK,
            user=user,
        )
        _move(
            lot,
            Reason.RETURN,
            take,
            value,
            user=user,
            entered=(entered, unit) if unit != material.unit else None,
            source_type=USAGE,
            source_id=move.source_id,
            note=f"{wo.number}: returned",
        )
        usages.append(usage)
    return usages


# ---------------------------------------------------------------------------
# Reports and checks
# ---------------------------------------------------------------------------


def items_with_totals(qs=None):
    qs = qs if qs is not None else StockItem.objects.all()
    return qs.select_related("material", "default_location").annotate(
        on_hand=Coalesce(Sum("lots__qty_on_hand"), ZERO),
        value=Coalesce(Sum("lots__value_on_hand"), ZERO),
    )


def restock_list():
    """Tracked items at or below their restock level."""
    return (
        items_with_totals(StockItem.objects.filter(tracked=True, min_quantity__isnull=False))
        .filter(on_hand__lte=F("min_quantity"))
        .order_by("material__name")
    )


def notify_low_stock() -> int:
    """Email staff about materials newly at or below their restock level.

    Each material is mentioned once until it goes back above its level, so a
    material that stays low isn't repeated every hour. Returns how many were new.
    """
    from django.conf import settings
    from django.urls import reverse

    from trammel.core.templatetags.trammel import qty
    from trammel.notifications.services import notify

    low = list(restock_list())
    StockItem.objects.filter(low_notified_at__isnull=False).exclude(
        pk__in=[i.pk for i in low]
    ).update(low_notified_at=None)
    new = [i for i in low if i.low_notified_at is None]
    if not new:
        return 0

    def amount(value, item):
        return f"{qty(value)} {item.material.get_unit_display()}" if value is not None else ""

    notify(
        "stock_low",
        context={
            "items": [
                {
                    "material": str(i.material),
                    "on_hand": amount(i.on_hand, i),
                    "restock_at": amount(i.min_quantity, i),
                    "reorder": amount(i.reorder_quantity, i),
                }
                for i in new
            ],
            "link": f"{settings.BASE_URL}{reverse('stock:restock')}",
        },
    )
    StockItem.objects.filter(pk__in=[i.pk for i in new]).update(low_notified_at=timezone.now())
    return len(new)


def check_ledger() -> list[str]:
    """Every lot's running totals equal the sum of its ledger moves."""
    problems = []
    sums = {
        row["lot"]: (row["q"], row["v"])
        for row in StockMove.objects.values("lot").annotate(q=Sum("quantity"), v=Sum("value"))
    }
    for lot in StockLot.objects.select_related("item__material"):
        q, v = sums.get(lot.pk, (ZERO, ZERO))
        if q != lot.qty_on_hand or v != lot.value_on_hand:
            problems.append(
                f"{lot.number} ({lot.item.material}): lot shows {lot.qty_on_hand} worth "
                f"{lot.value_on_hand}, ledger adds up to {q} worth {v}."
            )
    from trammel.workorders.models import MaterialUsage

    drawn = {
        row["source_id"]: row["v"]
        for row in StockMove.objects.filter(
            reason__in=(Reason.CONSUME, Reason.REMNANT), source_type=USAGE
        )
        .values("source_id")
        .annotate(v=Sum("value"))
    }
    for usage in MaterialUsage.objects.filter(pk__in=drawn.keys()):
        if usage.total_cost != -drawn[usage.pk]:
            problems.append(
                f"{usage.work_order.number}: usage of {usage.description} costs "
                f"{usage.total_cost}, the stock it drew was worth {-drawn[usage.pk]}."
            )
    return problems


def valuation(as_of, group: str = "material") -> tuple[list[dict], dict]:
    """What stock was on hand and worth at the end of `as_of`, rebuilt from the ledger
    (moves count from when they were recorded). Grouped by material, category or
    location; quantities are only added up within a material."""
    moves = StockMove.objects.filter(created_at__date__lte=as_of)
    if group == "location":
        keys = ["location__path"]
    elif group == "category":
        keys = ["item__material__category"]
    else:
        keys = ["item__material__name", "item__material__unit", "item__material_id"]
    rows = []
    for row in moves.values(*keys).annotate(q=Sum("quantity"), v=Sum("value")).order_by(*keys):
        if not row["q"] and not row["v"]:
            continue
        rows.append({"key": [row[k] for k in keys], "quantity": row["q"], "value": row["v"]})
    if group == "category":
        labels = dict(Material.Category.choices)
        for r in rows:
            r["key"][0] = labels.get(r["key"][0], r["key"][0])
    return rows, {"value": sum((r["value"] for r in rows), ZERO)}


#: Reasons that change what is on hand without a purchase or a job.
ADJUSTMENTS = (Reason.ADJUST, Reason.COUNT, Reason.SCRAP, Reason.UNRECEIVE)


def adjustments(start, end, group: str = "reason") -> tuple[list[dict], list, dict]:
    """Stock found, counted, scrapped and taken back out in a period: (summary rows,
    the moves themselves, totals)."""
    moves = StockMove.objects.filter(
        reason__in=ADJUSTMENTS, created_at__date__range=(start, end)
    ).select_related("item__material", "lot", "location", "user")
    keys = ["reason"] if group == "reason" else ["item__material__name", "item__material__unit"]
    summary = []
    for row in moves.values(*keys).annotate(q=Sum("quantity"), v=Sum("value")).order_by(*keys):
        if group == "reason":
            label = Reason(row["reason"]).label
            summary.append({"name": label, "unit": "", "quantity": None, "value": row["v"]})
        else:
            summary.append(
                {
                    "name": row["item__material__name"],
                    "unit": row["item__material__unit"],
                    "quantity": row["q"],
                    "value": row["v"],
                }
            )
    detail = list(moves.order_by("-created_at")[:500])
    sums = moves.aggregate(
        lost=Coalesce(Sum("value", filter=Q(value__lt=0)), ZERO),
        found=Coalesce(Sum("value", filter=Q(value__gt=0)), ZERO),
    )
    return summary, detail, {**sums, "net": sums["lost"] + sums["found"]}


# ---------------------------------------------------------------------------
# Bills of materials (FR-STK-6)
# ---------------------------------------------------------------------------


@transaction.atomic
def save_bom_line(line) -> object:
    """Add or change one component of a product's bill of materials."""
    from .models import BOMLine

    try:
        line.material.per_stock_unit(line.unit)
    except units.IncompatibleUnits as exc:
        raise ValidationError({"unit": str(exc)}) from None
    if line.quantity is None or line.quantity <= 0:
        raise ValidationError({"quantity": "Enter how much goes into one."})
    if line.step_id and line.step.product_id != line.product_id:
        raise ValidationError({"step": "That step is in another product's routing."})
    adding = line._state.adding
    if adding and not line.position:
        last = BOMLine.objects.filter(product=line.product).aggregate(m=Max("position"))["m"]
        line.position = (last or 0) + 10
    line.save()
    audit.record(
        "product.bom",
        line.product,
        summary=f"{line.product}: {'added' if adding else 'changed to'} "
        f"{line.quantity} {line.unit} {line.material}",
    )
    costing.refresh([line.product])
    return line


def bom_cost(product) -> tuple[list, Decimal]:
    """The product's bill of materials, each line with its `cost` at current
    material costs (None if its unit doesn't convert), and their total for one."""
    lines = list(product.bom_lines.select_related("material"))
    total = ZERO
    for line in lines:
        try:
            line.cost = line.material.cost_per(line.unit) * line.quantity
        except units.IncompatibleUnits:
            line.cost = None
        total += line.cost or ZERO
    return lines, total.quantize(Q4)


@transaction.atomic
def delete_bom_line(line) -> None:
    product, label = line.product, str(line)
    line.delete()
    audit.record("product.bom_delete", product, summary=f"{product}: removed {label}")
    costing.refresh([product])


def refresh_costs_using(material) -> int:
    """Products whose bill of materials uses `material`, after its cost changed."""
    from trammel.catalog.models import Product

    return costing.refresh(Product.objects.filter(bom_lines__material=material).distinct())


def plan_bom(wo, line, operation, steps=None) -> None:
    """A product line carried over to a work order: plan each component of its
    bill of materials, times the quantity, on the operation for the routing step
    that uses it (`steps` maps step ids to them), else on the line's operation."""
    from trammel.workorders.models import PlannedMaterial

    product = getattr(line, "product", None)
    if product is None:
        return
    steps = steps or {}
    for part in product.bom_lines.select_related("material"):
        PlannedMaterial.objects.create(
            work_order=wo,
            operation=steps.get(part.step_id, operation),
            material=part.material,
            description=f"{part.material} (for {product.name})",
            quantity=(part.quantity * line.quantity).quantize(Q4),
            unit=part.unit,
        )
