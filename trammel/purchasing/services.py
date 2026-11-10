"""Purchase order writes (FR-PUR-1..5). Web and API call these.

Landed cost: the order's charges (tax + shipping - discount) are spread over its
lines in proportion to their value, in whole cents, with leftover cents going
to the lines with the largest remainders, so the lines add up to the order
exactly. A delivery brings in its share of its line's landed cost; the delivery
that completes a line brings in exactly what is left of it.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_DOWN, Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from trammel.core import audit, numbering, units
from trammel.core.money import round_money
from trammel.core.services import save_audited
from trammel.shop.models import Material
from trammel.stock import services as stock
from trammel.stock.models import Location, StockLot

from .models import POLine, PurchaseOrder, PurchaseReceipt, Status, VendorItem

ZERO = Decimal("0")
CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# Landed cost
# ---------------------------------------------------------------------------


def allocate(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Split `total` (money) over `weights` in whole cents, largest remainder first.
    Equal shares when every weight is zero. The shares add up to `total` exactly."""
    if not weights:
        return []
    if total == 0:
        return [ZERO for _ in weights]
    sign = -1 if total < 0 else 1
    cents = int((abs(total) / CENT).to_integral_value())
    if sum(weights) <= 0:
        weights = [Decimal("1")] * len(weights)
    whole = sum(weights)
    exact = [Decimal(cents) * w / whole for w in weights]
    floor = [int(x.to_integral_value(rounding=ROUND_DOWN)) for x in exact]
    left = cents - sum(floor)
    order = sorted(range(len(weights)), key=lambda i: (exact[i] - floor[i], -i), reverse=True)
    for i in order[:left]:
        floor[i] += 1
    return [sign * Decimal(c) * CENT for c in floor]


def landed(po: PurchaseOrder) -> dict:
    """Each line's landed cost: its goods, its share of shipping less the discount,
    and its share of the tax. Tax-exempt lines take no tax; if every line is
    exempt (placing such an order is refused), the tax is spread over them all
    rather than lost."""
    lines = list(po.lines.all())
    out = {line.pk: line.goods for line in lines}
    for line, share in zip(
        lines, allocate(po.shipping - po.discount, [x.goods for x in lines]), strict=True
    ):
        out[line.pk] += share
    taxable = [x for x in lines if not x.tax_exempt] or lines
    for line, share in zip(taxable, allocate(po.tax, [x.goods for x in taxable]), strict=True):
        out[line.pk] += share
    return out


def totals(po: PurchaseOrder) -> dict:
    goods = sum((line.goods for line in po.lines.all()), ZERO)
    return {"goods": goods, "charges": po.charges, "total": goods + po.charges}


def received(line: POLine) -> tuple[Decimal, Decimal]:
    """(quantity, landed value) received on a line so far, net of undone receipts."""
    row = line.receipts.aggregate(q=models.Sum("quantity"), v=models.Sum("value"))
    return row["q"] or ZERO, row["v"] or ZERO


def line_status(line: POLine) -> str:
    qty, _ = received(line)
    if qty <= 0:
        return "none"
    return "all" if qty >= line.quantity else "part"


def receipt_state(po: PurchaseOrder) -> str:
    """Nothing, part or all of the order received — worked out from the lines."""
    states = {line_status(line) for line in po.lines.all()}
    if not states or states == {"none"}:
        return "none"
    return "all" if states == {"all"} else "part"


# ---------------------------------------------------------------------------
# Drafting and ordering
# ---------------------------------------------------------------------------


def _require_editable(po: PurchaseOrder) -> None:
    if not po.editable:
        if po.status in (Status.CLOSED, Status.CANCELLED):
            raise ValidationError(f"{po} is {po.get_status_display().lower()}.")
        raise ValidationError(f"{po} has deliveries received, so its lines and charges are fixed.")


@transaction.atomic
def create_order(vendor, **fields) -> PurchaseOrder:
    po = PurchaseOrder(vendor=vendor, **fields)
    return save_audited(po, summary=f"Drafted a purchase order to {vendor}")


@transaction.atomic
def update_order(po: PurchaseOrder, **fields) -> PurchaseOrder:
    charges = {"tax", "shipping", "discount", "tax_exempt"}
    if charges & {k for k, v in fields.items() if getattr(po, k) != v}:
        _require_editable(po)
    before = audit.snapshot(po)
    for key, value in fields.items():
        setattr(po, key, value)
    return save_audited(po, before=before)


def suggest(vendor, material: Material) -> dict:
    """What a new line for `material` starts as: the vendor's SKU and last price."""
    item = VendorItem.objects.filter(vendor=vendor, material=material).first()
    if item and item.last_price is not None:
        return {
            "vendor_sku": item.vendor_sku,
            "unit_price": item.last_price,
            "unit": item.last_unit or material.unit,
        }
    return {
        "vendor_sku": item.vendor_sku if item else material.sku,
        "unit_price": material.unit_cost,
        "unit": material.unit,
    }


@transaction.atomic
def save_line(line: POLine) -> POLine:
    po = line.order
    _require_editable(po)
    if line.material is not None:
        try:
            line.material.per_stock_unit(line.unit)
        except units.IncompatibleUnits as exc:
            raise ValidationError({"unit": str(exc)}) from None
        line.description = line.description or str(line.material)
    if not line.description.strip():
        raise ValidationError({"description": "Pick a material or describe the item."})
    if line.work_order is not None and not line.work_order.is_editable:
        raise ValidationError({"work_order": f"{line.work_order.number} is closed."})
    if line._state.adding and not line.position:
        last = po.lines.aggregate(m=models.Max("position"))["m"] or 0
        line.position = last + 10
    line.full_clean(exclude=["order"])
    line.save()
    audit.record("purchase_order.line", po, summary=f"{po}: {line.quantity} {line.unit} {line}")
    return line


@transaction.atomic
def delete_line(line: POLine) -> None:
    po = line.order
    _require_editable(po)
    name = str(line)
    line.delete()
    audit.record("purchase_order.line_delete", po, summary=f"{po}: removed {name}")


@transaction.atomic
def place(po: PurchaseOrder, *, actor, ordered_on: dt.date | None = None) -> PurchaseOrder:
    """Send the order: it gets its number, and vendor prices are remembered."""
    PurchaseOrder.lifecycle.apply(po, Status.ORDERED, actor=actor)
    po.refresh_from_db()
    if not po.number:
        po.number = numbering.next_number("purchase_order")
    po.ordered_on = ordered_on or timezone.localdate()
    PurchaseOrder.objects.filter(pk=po.pk).update(number=po.number, ordered_on=po.ordered_on)
    for line in po.lines.exclude(material=None):
        VendorItem.objects.update_or_create(
            vendor=po.vendor,
            material=line.material,
            defaults={
                "vendor_sku": line.vendor_sku,
                "last_price": line.unit_price,
                "last_unit": line.unit,
                "last_ordered_on": po.ordered_on,
            },
        )
    audit.record("purchase_order.order", po, summary=f"Ordered {po.number} from {po.vendor}")
    return po


def transition(po: PurchaseOrder, target: str, *, actor, reason: str = "") -> None:
    t = PurchaseOrder.lifecycle.find(po, target)
    kind = t.kind if t else None
    PurchaseOrder.lifecycle.apply(po, target, actor=actor, reason=reason, kind=kind)


# ---------------------------------------------------------------------------
# Receiving
# ---------------------------------------------------------------------------


@transaction.atomic
def receive(
    line: POLine,
    *,
    quantity: Decimal,
    location: Location | None = None,
    received_on: dt.date | None = None,
    user=None,
) -> PurchaseReceipt:
    """Part or all of a line arrives (FR-PUR-2). Stock comes in as a lot at its
    landed cost; a line bought for a job goes onto the work order."""
    po = PurchaseOrder.objects.select_for_update().get(pk=line.order_id)
    line = POLine.objects.select_related("material", "work_order").get(pk=line.pk)
    if po.status != Status.ORDERED:
        raise ValidationError(f"{po} is {po.get_status_display().lower()}; place it first.")
    quantity = Decimal(quantity)
    if quantity <= 0:
        raise ValidationError("Enter how much arrived.")
    got, got_value = received(line)
    if got + quantity > line.quantity:
        raise ValidationError(
            f"Only {stock.fmt(line.quantity - got, line.unit)} of {line} is still to come. "
            "Receive any extra on the material's stock page."
        )
    line_landed = landed(po)[line.pk]
    if got + quantity == line.quantity:
        value = line_landed - got_value
    else:
        value = round_money(line_landed * quantity / line.quantity)
    received_on = received_on or timezone.localdate()
    material = line.material
    tracked = material is not None and stock.tracked_item(material) is not None
    lot, previous = None, None
    if tracked:
        previous = Material.objects.get(pk=material.pk).unit_cost
        lot = stock.receive(
            material,
            quantity=quantity,
            unit=line.unit,
            location=location,
            total_cost=value,
            received_on=received_on,
            vendor=po.vendor,
            note=f"{po.number}: {line}",
            update_cost=line.update_cost,
            user=user,
        )
    receipt = PurchaseReceipt.objects.create(
        order=po,
        line=line,
        quantity=quantity,
        value=value,
        received_on=received_on,
        lot=lot,
        previous_unit_cost=previous,
        user=user,
    )
    if line.work_order is not None:
        _to_work_order(po, line, receipt, quantity, value, user)
    if receipt_state(po) == "all":
        from trammel.core.access import SYSTEM

        PurchaseOrder.lifecycle.apply(po, Status.CLOSED, actor=SYSTEM)
    audit.record(
        "purchase_order.receive",
        po,
        summary=f"{po.number}: received {stock.fmt(quantity, line.unit)} of {line}",
    )
    return receipt


def _to_work_order(po, line, receipt, quantity, value, user) -> None:
    """A line bought for a job goes straight onto its work order, at landed cost."""
    from trammel.workorders import services as wos
    from trammel.workorders.models import MaterialUsage

    wo = line.work_order
    if receipt.lot is not None:
        wos.record_material(
            wo,
            material=line.material,
            quantity=quantity,
            unit=line.unit,
            lot=receipt.lot.pk,
            note=f"Bought on {po.number}",
            user=user,
        )
    else:
        wos.write_usage(
            wo,
            material=line.material,
            description=line.description,
            quantity=quantity,
            unit=line.unit,
            unit_cost=(value / quantity).quantize(Decimal("0.0001")),
            total_cost=value,
            note=f"Bought on {po.number}",
            source=MaterialUsage.Source.MANUAL,
            user=user,
        )
    wos.add_note(
        wo, text=f"Arrived: {stock.fmt(quantity, line.unit)} of {line} ({po.number}).", user=user
    )


@transaction.atomic
def unreceive(receipt: PurchaseReceipt, *, user=None, reason: str = "") -> PurchaseReceipt:
    """Undo a delivery entered by mistake (FR-PUR-3): only while its stock is still
    all there, untouched. The material's cost goes back to what it was."""
    receipt = PurchaseReceipt.objects.select_related("line__material", "order", "lot").get(
        pk=receipt.pk
    )
    po, line = receipt.order, receipt.line
    if receipt.quantity <= 0 or hasattr(receipt, "reversed_by"):
        raise ValidationError("That receipt has already been undone.")
    if line.work_order is not None:
        raise ValidationError(
            f"It went onto {line.work_order.number}. Return it there first, then undo the receipt."
        )
    lot = None
    if receipt.lot is not None:
        lot = StockLot.objects.select_for_update().get(pk=receipt.lot_id)
        if lot.qty_on_hand != lot.moves.filter(reason="receive").first().quantity or (
            lot.value_on_hand != receipt.value
        ):
            raise ValidationError(
                f"Some of {lot.number} has been used, moved or counted since it arrived. "
                "Put that right first; a receipt is undone only while its stock is untouched."
            )
        stock.unreceive_lot(lot, note=f"{po}: receipt undone", user=user)
        material = line.material
        if receipt.previous_unit_cost is not None and line.update_cost:
            current = Material.objects.get(pk=material.pk).unit_cost
            if current == lot.unit_cost and current != receipt.previous_unit_cost:
                Material.objects.filter(pk=material.pk).update(unit_cost=receipt.previous_unit_cost)
                audit.record(
                    "material.cost",
                    material,
                    summary=f"{material}: cost back to {receipt.previous_unit_cost} "
                    f"({po} receipt undone)",
                )
    reversal = PurchaseReceipt.objects.create(
        order=po,
        line=line,
        quantity=-receipt.quantity,
        value=-receipt.value,
        received_on=timezone.localdate(),
        lot=lot,
        reverses=receipt,
        user=user,
    )
    if po.status == Status.CLOSED:
        from trammel.core.access import SYSTEM

        PurchaseOrder.lifecycle.apply(
            po, Status.ORDERED, actor=SYSTEM, kind="reopen", reason="A receipt was undone"
        )
    audit.record(
        "purchase_order.unreceive",
        po,
        summary=f"{po}: undid receipt of {stock.fmt(receipt.quantity, line.unit)} of {line}",
        reason=reason,
    )
    return reversal


# ---------------------------------------------------------------------------
# What is on order, and ordering what is low
# ---------------------------------------------------------------------------


def on_order(item_ids) -> dict:
    """Still to come on placed orders, per stock item, in the stock unit."""
    from trammel.stock.models import StockItem

    items = {i.material_id: i.pk for i in StockItem.objects.filter(pk__in=item_ids)}
    if not items:
        return {}
    out: dict = {}
    lines = POLine.objects.filter(
        order__status=Status.ORDERED, order__deleted_at=None, material_id__in=items
    ).select_related("material")
    for line in lines:
        got, _ = received(line)
        left = line.quantity - got
        if left <= 0:
            continue
        try:
            qty = line.material.to_stock(left, line.unit)
        except units.IncompatibleUnits:
            continue
        key = items[line.material_id]
        out[key] = out.get(key, ZERO) + qty
    return out


@transaction.atomic
def orders_for_restock(items, *, user=None) -> tuple[list[PurchaseOrder], list]:
    """Draft one order per vendor for the stock items given, each line the reorder
    quantity (or enough to reach the restock level). Items with no vendor are
    returned so the person can be told."""
    by_vendor: dict = {}
    skipped = []
    for item in items:
        material = item.material
        if material.vendor is None:
            skipped.append(item)
            continue
        by_vendor.setdefault(material.vendor, []).append(item)
    orders = []
    for vendor, group in by_vendor.items():
        po = create_order(vendor, notes="Drafted from the restock list.")
        for item in group:
            material = item.material
            on_hand = stock.on_hand(item)
            need = item.reorder_quantity or max((item.min_quantity or ZERO) - on_hand, Decimal("1"))
            start = suggest(vendor, material)
            unit = start["unit"]
            try:
                qty = material.convert(need, material.unit, unit)
            except units.IncompatibleUnits:
                unit, qty = material.unit, need
            save_line(
                POLine(
                    order=po,
                    material=material,
                    description=str(material),
                    vendor_sku=start["vendor_sku"],
                    quantity=qty.quantize(Decimal("0.0001")),
                    unit=unit,
                    unit_price=start["unit_price"],
                )
            )
        orders.append(po)
    return orders, skipped


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def spend(start: dt.date, end: dt.date, group: str = "vendor") -> tuple[list[dict], dict]:
    """Orders placed in a period (canceled ones left out). By vendor or month, the
    goods, tax, shipping, discount and total; by material, each line's quantity
    and landed cost."""
    orders = list(
        PurchaseOrder.objects.filter(ordered_on__range=(start, end))
        .exclude(status=Status.CANCELLED)
        .select_related("vendor")
        .prefetch_related("lines__material")
    )
    rows: dict = {}
    fields = ("goods", "tax", "shipping", "discount", "total")
    if group == "material":
        for po in orders:
            landed_by_line = landed(po)
            for line in po.lines.all():
                key = (line.description, line.unit)
                r = rows.setdefault(
                    key,
                    {
                        "name": key[0],
                        "unit": key[1],
                        "quantity": ZERO,
                        "goods": ZERO,
                        "landed": ZERO,
                        "orders": set(),
                    },
                )
                r["quantity"] += line.quantity
                r["goods"] += line.goods
                r["landed"] += landed_by_line[line.pk]
                r["orders"].add(po.pk)
        out = sorted(rows.values(), key=lambda r: -r["landed"])
        for r in out:
            r["orders"] = len(r["orders"])
        return out, {
            "goods": sum((r["goods"] for r in out), ZERO),
            "landed": sum((r["landed"] for r in out), ZERO),
        }
    for po in orders:
        if group == "month":
            key = po.ordered_on.replace(day=1)
            name = key
        else:
            key, name = po.vendor_id, po.vendor.name
        r = rows.setdefault(key, {"name": name, "orders": 0, **{f: ZERO for f in fields}})
        t = totals(po)
        r["orders"] += 1
        r["goods"] += t["goods"]
        r["tax"] += po.tax
        r["shipping"] += po.shipping
        r["discount"] += po.discount
        r["total"] += t["total"]
    if group == "month":
        out = [rows[k] for k in sorted(rows)]
    else:
        out = sorted(rows.values(), key=lambda r: -r["total"])
    return out, {f: sum((r[f] for r in out), ZERO) for f in fields}


def open_orders() -> list[PurchaseOrder]:
    """Orders placed and not yet all here, the late ones first, each with what is
    still to come and its landed value."""
    today = timezone.localdate()
    out = []
    for po in PurchaseOrder.objects.filter(status=Status.ORDERED).select_related("vendor"):
        landed_by_line = landed(po)
        to_come = ZERO
        lines_left = 0
        for line in po.lines.all():
            got, value = received(line)
            if got < line.quantity:
                lines_left += 1
                to_come += landed_by_line[line.pk] - value
        if not lines_left:
            continue
        po.to_come, po.lines_left = to_come, lines_left
        po.late = po.expected_on is not None and po.expected_on < today
        po.days_late = (today - po.expected_on).days if po.late else 0
        out.append(po)
    return sorted(
        out, key=lambda p: (not p.late, p.expected_on or dt.date.max, p.ordered_on or dt.date.max)
    )


def price_history(material=None, vendor=None, limit: int = 500) -> list[POLine]:
    """What was paid, order by order: the price and the landed cost per unit."""
    lines = (
        POLine.objects.filter(order__ordered_on__isnull=False)
        .exclude(order__status=Status.CANCELLED)
        .select_related("order__vendor", "material")
        .order_by("material__name", "description", "-order__ordered_on")
    )
    if material is not None:
        lines = lines.filter(material=material)
    if vendor is not None:
        lines = lines.filter(order__vendor=vendor)
    lines = list(lines[:limit])
    cache: dict = {}
    for line in lines:
        if line.order_id not in cache:
            cache[line.order_id] = landed(line.order)
        line.landed = cache[line.order_id][line.pk]
        line.landed_unit = (line.landed / line.quantity).quantize(Decimal("0.0001"))
    return lines
