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

from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from trammel.core import audit, numbering, units
from trammel.core.money import round_money
from trammel.core.services import save_audited
from trammel.shop.models import Material

from .models import Location, Reason, StockItem, StockLot, StockMove, StockSettings

ZERO = Decimal("0")
Q4 = Decimal("0.0001")
USAGE = "workorders.materialusage"


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


@transaction.atomic
def delete_location(location: Location) -> None:
    if StockLot.objects.filter(location=location, qty_on_hand__gt=0).exists():
        raise ValidationError(f"{location} still holds stock. Move it first.")
    if Location.objects.filter(parent=location).exists():
        raise ValidationError(f"{location} has locations inside it. Move or delete them first.")
    if StockItem.objects.filter(default_location=location).exists():
        raise ValidationError(f"{location} is the default location of some materials.")
    from trammel.core.services import delete_audited

    delete_audited(location)


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------


def item_for(material: Material) -> StockItem | None:
    return StockItem.objects.filter(material=material).first()


def tracked_item(material: Material) -> StockItem | None:
    return StockItem.objects.filter(material=material, tracked=True).first()


def on_hand(item: StockItem, location: Location | None = None) -> Decimal:
    lots = item.lots.all() if location is None else item.lots.filter(location=location)
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
) -> StockLot:
    lot = StockLot.objects.create(
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
    here = on_hand(item, location)
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
            item=item, location=location, qty_on_hand__gt=0
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
    description: str = "",
    note: str = "",
    billable: bool = True,
    user=None,
) -> list:
    """Use stock on a work order: oldest lots first, or the lot given. Writes one
    usage per lot drawn from, at that lot's cost (FR-STK-3)."""
    from trammel.workorders import services as wo_services
    from trammel.workorders.models import MaterialUsage

    item = _lock(_require_tracked(item_for(material), material))
    unit = unit or material.unit
    quantity = Decimal(quantity)
    qty = _stock_quantity(material, quantity, unit)
    lots = StockLot.objects.select_for_update().filter(item=item, qty_on_hand__gt=0)
    if lot is not None:
        lots = lots.filter(pk=getattr(lot, "pk", lot))
        if not lots.exists():
            raise ValidationError(f"That lot has no {material} left.")
    lots = list(lots.order_by("received_on", "created_at", "id"))
    available = sum((x.qty_on_hand for x in lots), ZERO)
    if qty > available:
        short = qty - available
        if lot is not None:
            raise ValidationError(
                f"{lots[0].number} has only {fmt(available, material.unit)} of {material}."
            )
        if StockSettings.load().shortfall != StockSettings.Shortfall.FOUND:
            raise ValidationError(
                f"Only {fmt(available, material.unit)} of {material} is in stock, and "
                f"{fmt(qty, material.unit)} is needed. Count or receive it first."
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
    for n, (x, take) in enumerate(picks):
        value = _draw_value(x, take)
        if n == len(picks) - 1:
            entered = quantity - entered_so_far
        else:
            entered = _q(material.convert(take, material.unit, unit))
            entered_so_far += entered
        usage = wo_services.write_usage(
            wo,
            material=material,
            description=description,
            quantity=entered,
            unit=unit,
            unit_cost=_q(value / entered) if entered else ZERO,
            total_cost=value,
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
        usages.append(usage)
    return usages


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
            item=item, reason=Reason.RETURN, source_type=USAGE, source_id__in=usage_ids
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
        for row in StockMove.objects.filter(reason=Reason.CONSUME, source_type=USAGE)
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
