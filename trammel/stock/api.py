"""Stock API: what is where, the ledger, and the same actions as the web pages."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

from django.http import Http404
from django.shortcuts import get_object_or_404
from ninja import Router, Schema, Status

from trammel.core import access
from trammel.shop.models import Material, Vendor

from . import services
from .models import Location, StockLot, StockMove

router = Router(tags=["stock"])


class LocationOut(Schema):
    id: UUID
    name: str
    path: str
    parent: UUID | None
    is_active: bool

    @staticmethod
    def resolve_parent(obj):
        return obj.parent_id


class ItemOut(Schema):
    material: UUID
    name: str
    unit: str
    tracked: bool
    on_hand: Decimal
    min_quantity: Decimal | None
    reorder_quantity: Decimal | None
    default_location: UUID | None

    @staticmethod
    def resolve_material(obj):
        return obj.material_id

    @staticmethod
    def resolve_name(obj):
        return str(obj.material)

    @staticmethod
    def resolve_unit(obj):
        return obj.material.unit

    @staticmethod
    def resolve_default_location(obj):
        return obj.default_location_id


class LotOut(Schema):
    id: UUID
    number: str
    material: UUID
    location: UUID
    location_path: str
    received_on: dt.date
    qty_on_hand: Decimal
    unit: str

    @staticmethod
    def resolve_material(obj):
        return obj.item.material_id

    @staticmethod
    def resolve_location(obj):
        return obj.location_id

    @staticmethod
    def resolve_location_path(obj):
        return obj.location.path

    @staticmethod
    def resolve_unit(obj):
        return obj.item.material.unit


class MoveOut(Schema):
    id: UUID
    created_at: dt.datetime
    lot: str
    material: UUID
    location: UUID
    reason: str
    quantity: Decimal
    note: str

    @staticmethod
    def resolve_lot(obj):
        return obj.lot.number

    @staticmethod
    def resolve_material(obj):
        return obj.item.material_id

    @staticmethod
    def resolve_location(obj):
        return obj.location_id


class ReceiveIn(Schema):
    material: UUID
    quantity: Decimal
    unit: str = ""
    location: UUID | None = None
    unit_cost: Decimal | None = None
    total_cost: Decimal | None = None
    received_on: dt.date | None = None
    vendor: UUID | None = None
    note: str = ""
    opening: bool = False
    update_cost: bool = True


class CountIn(Schema):
    material: UUID
    location: UUID
    counted: Decimal
    unit: str = ""
    #: What the counter was shown; the count is refused if stock has changed since.
    expected: Decimal | None = None
    note: str = ""


class LotActionIn(Schema):
    quantity: Decimal
    unit: str = ""
    to: UUID | None = None
    note: str = ""


def _view(request):
    access.require(request.actor, "stock.view")


def _material(request, pk) -> Material:
    return access.get_or_404(request.actor, "shop.view", Material.objects.all(), pk=pk)


def _location(pk) -> Location | None:
    return get_object_or_404(Location, pk=pk) if pk else None


@router.get("/locations", response=list[LocationOut])
def list_locations(request):
    _view(request)
    return Location.objects.all()


@router.get("/items", response=list[ItemOut])
def list_items(request):
    _view(request)
    return services.items_with_totals().order_by("material__name")


@router.get("/lots", response=list[LotOut])
def list_lots(request, material: UUID | None = None, location: UUID | None = None):
    _view(request)
    qs = StockLot.objects.filter(qty_on_hand__gt=0).select_related("item__material", "location")
    if material:
        qs = qs.filter(item__material_id=material)
    if location:
        qs = qs.filter(location_id=location)
    return qs


@router.get("/moves", response=list[MoveOut])
def list_moves(request, material: UUID | None = None, limit: int = 100):
    _view(request)
    qs = StockMove.objects.select_related("lot", "item").order_by("-created_at", "-id")
    if material:
        qs = qs.filter(item__material_id=material)
    return qs[: max(1, min(limit, 1000))]


@router.post("/receive", response={201: LotOut})
def receive(request, payload: ReceiveIn):
    access.require(request.actor, "stock.edit")
    lot = services.receive(
        _material(request, payload.material),
        quantity=payload.quantity,
        unit=payload.unit,
        location=_location(payload.location),
        unit_cost=payload.unit_cost,
        total_cost=payload.total_cost,
        received_on=payload.received_on,
        vendor=get_object_or_404(Vendor, pk=payload.vendor) if payload.vendor else None,
        note=payload.note,
        opening=payload.opening,
        update_cost=payload.update_cost,
        user=request.actor.user,
    )
    return Status(201, lot)


@router.post("/count", response=dict)
def count(request, payload: CountIn):
    access.require(request.actor, "stock.edit")
    diff = services.count(
        _material(request, payload.material),
        location=_location(payload.location),
        counted=payload.counted,
        unit=payload.unit,
        expected=payload.expected,
        note=payload.note,
        user=request.actor.user,
    )
    return {"difference": str(diff)}


@router.post("/lots/{lot_id}/{action}", response=dict)
def lot_action(request, lot_id: UUID, action: str, payload: LotActionIn):
    access.require(request.actor, "stock.edit")
    lot = get_object_or_404(StockLot, pk=lot_id)
    if action == "transfer":
        if payload.to is None:
            from django.core.exceptions import ValidationError

            raise ValidationError("Say where it goes.")
        moved = services.transfer(
            lot,
            quantity=payload.quantity,
            unit=payload.unit,
            to=_location(payload.to),
            note=payload.note,
            user=request.actor.user,
        )
        return {"lot": str(moved.pk), "number": moved.number}
    if action == "scrap":
        services.scrap(
            lot,
            quantity=payload.quantity,
            unit=payload.unit,
            note=payload.note,
            user=request.actor.user,
        )
        return {"lot": str(lot.pk), "number": lot.number}
    raise Http404


# ---------------------------------------------------------------------------
# Bills of materials (FR-STK-6)
# ---------------------------------------------------------------------------


class BOMLineOut(Schema):
    id: UUID
    material: UUID
    material_name: str
    quantity: Decimal
    unit: str
    note: str
    position: int

    @staticmethod
    def resolve_material(obj):
        return obj.material_id

    @staticmethod
    def resolve_material_name(obj):
        return str(obj.material)


class BOMLineIn(Schema):
    material: UUID
    quantity: Decimal
    unit: str = ""
    note: str = ""
    #: The routing step that uses it, if any.
    step: UUID | None = None


def _product(request, perm, pk):
    from trammel.catalog.models import Product

    return access.get_or_404(request.actor, perm, Product.objects.all(), pk=pk)


def _bom_line(request, line_id):
    from .models import BOMLine

    line = get_object_or_404(BOMLine.objects.select_related("product"), pk=line_id)
    _product(request, "catalog.edit", line.product_id)
    return line


def _fill(line, payload: BOMLineIn):
    line.material = get_object_or_404(Material, pk=payload.material)
    line.quantity = payload.quantity
    line.unit = payload.unit or line.material.unit
    line.note = payload.note[:200]
    line.step = (
        get_object_or_404(line.product.routing_steps.all(), pk=payload.step)
        if payload.step
        else None
    )
    return line


@router.get("/products/{product_id}/bom", response=list[BOMLineOut])
def list_bom(request, product_id: UUID):
    return _product(request, "catalog.view", product_id).bom_lines.select_related("material")


@router.post("/products/{product_id}/bom", response={201: BOMLineOut})
def add_bom_line(request, product_id: UUID, payload: BOMLineIn):
    from .models import BOMLine

    product = _product(request, "catalog.edit", product_id)
    return Status(201, services.save_bom_line(_fill(BOMLine(product=product), payload)))


@router.put("/bom/{line_id}", response=BOMLineOut)
def update_bom_line(request, line_id: UUID, payload: BOMLineIn):
    return services.save_bom_line(_fill(_bom_line(request, line_id), payload))


@router.delete("/bom/{line_id}", response={204: None})
def delete_bom_line(request, line_id: UUID):
    services.delete_bom_line(_bom_line(request, line_id))
    return Status(204, None)
