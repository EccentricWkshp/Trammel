"""Purchasing API: purchase orders, their lines, placing and receiving."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja import Router, Schema, Status

from trammel.core import access
from trammel.shop.models import Material, Vendor
from trammel.stock.models import Location
from trammel.workorders.models import WorkOrder

from . import services
from .models import POLine, PurchaseOrder

router = Router(tags=["purchasing"])


class LineOut(Schema):
    id: UUID
    material: UUID | None
    description: str
    vendor_sku: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    work_order: UUID | None
    tax_exempt: bool
    received: Decimal

    @staticmethod
    def resolve_material(obj):
        return obj.material_id

    @staticmethod
    def resolve_work_order(obj):
        return obj.work_order_id

    @staticmethod
    def resolve_received(obj):
        return services.received(obj)[0]


class OrderOut(Schema):
    id: UUID
    number: str
    vendor: UUID
    status: str
    ordered_on: dt.date | None
    expected_on: dt.date | None
    vendor_reference: str
    tax_exempt: bool
    exemption_number: str
    tax: Decimal
    shipping: Decimal
    discount: Decimal
    total: Decimal
    lines: list[LineOut]

    @staticmethod
    def resolve_vendor(obj):
        return obj.vendor_id

    @staticmethod
    def resolve_total(obj):
        return services.totals(obj)["total"]

    @staticmethod
    def resolve_lines(obj):
        return list(obj.lines.all())


class OrderIn(Schema):
    vendor: UUID
    expected_on: dt.date | None = None
    vendor_reference: str = ""
    tax_exempt: bool = False
    exemption_number: str = ""
    tax: Decimal = Decimal("0")
    shipping: Decimal = Decimal("0")
    discount: Decimal = Decimal("0")
    notes: str = ""


class LineIn(Schema):
    material: UUID | None = None
    description: str = ""
    vendor_sku: str = ""
    quantity: Decimal
    unit: str = ""
    #: Leave out to use the last price paid the vendor, or the material's cost.
    unit_price: Decimal | None = None
    work_order: UUID | None = None
    tax_exempt: bool = False
    update_cost: bool = True


class DeliveryIn(Schema):
    line: UUID
    quantity: Decimal
    location: UUID | None = None
    received_on: dt.date | None = None


def _po(request, perm, pk) -> PurchaseOrder:
    return access.get_or_404(request.actor, perm, PurchaseOrder.objects.all(), pk=pk)


@router.get("/orders", response=list[OrderOut])
def list_orders(request, status: str = ""):
    access.require(request.actor, "purchasing.view")
    qs = PurchaseOrder.objects.prefetch_related("lines")
    if status:
        qs = qs.filter(status=status)
    return qs


@router.get("/orders/{po_id}", response=OrderOut)
def get_order(request, po_id: UUID):
    return _po(request, "purchasing.view", po_id)


@router.post("/orders", response={201: OrderOut})
def create_order(request, payload: OrderIn):
    access.require(request.actor, "purchasing.create")
    data = payload.dict()
    vendor = get_object_or_404(Vendor, pk=data.pop("vendor"))
    return Status(201, services.create_order(vendor, **data))


@router.post("/orders/{po_id}/lines", response={201: LineOut})
def add_line(request, po_id: UUID, payload: LineIn):
    po = _po(request, "purchasing.create", po_id)
    material = get_object_or_404(Material, pk=payload.material) if payload.material else None
    start = services.suggest(po.vendor, material) if material else {}
    if material is None and payload.unit_price is None:
        from django.core.exceptions import ValidationError

        raise ValidationError({"unit_price": ["What does one unit cost?"]})
    line = POLine(
        order=po,
        material=material,
        description=payload.description,
        vendor_sku=payload.vendor_sku or start.get("vendor_sku", ""),
        quantity=payload.quantity,
        unit=payload.unit or start.get("unit") or "each",
        unit_price=payload.unit_price if payload.unit_price is not None else start["unit_price"],
        work_order=get_object_or_404(WorkOrder, pk=payload.work_order)
        if payload.work_order
        else None,
        tax_exempt=payload.tax_exempt,
        update_cost=payload.update_cost,
    )
    return Status(201, services.save_line(line))


@router.post("/orders/{po_id}/place", response=OrderOut)
def place(request, po_id: UUID):
    return services.place(_po(request, "purchasing.order", po_id), actor=request.actor)


@router.post("/orders/{po_id}/receive", response={201: dict})
def receive(request, po_id: UUID, payload: DeliveryIn):
    po = _po(request, "purchasing.receive", po_id)
    line = get_object_or_404(POLine, pk=payload.line, order=po)
    location = get_object_or_404(Location, pk=payload.location) if payload.location else None
    receipt = services.receive(
        line,
        quantity=payload.quantity,
        location=location,
        received_on=payload.received_on,
        user=request.actor.user,
    )
    return Status(
        201,
        {
            "id": str(receipt.pk),
            "value": str(receipt.value),
            "lot": str(receipt.lot_id) if receipt.lot_id else None,
        },
    )
