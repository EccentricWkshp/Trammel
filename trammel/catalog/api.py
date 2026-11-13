from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja import ModelSchema, Router, Schema, Status
from ninja.pagination import LimitOffsetPagination, paginate

from trammel.api.types import choices
from trammel.core import access, archive, audit
from trammel.shop.models import Machine

from . import costing, services
from .models import Product, RoutingStep

router = Router(tags=["products"])


class CostPartOut(Schema):
    label: str
    amount: Decimal
    detail: str


class ProductOut(ModelSchema):
    #: What one unit costs. The cost fields are null, and the breakdown empty,
    #: without permission to see costs (core.see_costs).
    cost: Decimal | None = None
    cost_override: Decimal | None = None
    other_cost: Decimal | None = None
    #: What one unit costs, calculated from these parts plus other cost, unless
    #: `cost_override` is set (then `cost` is the override).
    cost_breakdown: list[CostPartOut]

    class Meta:
        model = Product
        fields = [
            "id",
            "sku",
            "name",
            "kind",
            "description",
            "price",
            "batch_size",
            "unit",
            "tax_category",
            "is_active",
        ]

    @staticmethod
    def resolve_cost(obj, context):
        return obj.cost if access.sees_costs(context) else None

    @staticmethod
    def resolve_cost_override(obj, context):
        return obj.cost_override if access.sees_costs(context) else None

    @staticmethod
    def resolve_other_cost(obj, context):
        return obj.other_cost if access.sees_costs(context) else None

    @staticmethod
    def resolve_cost_breakdown(obj, context):
        if not access.sees_costs(context):
            return []
        return [
            {"label": p.label, "amount": p.amount, "detail": p.detail} for p in costing.parts(obj)
        ]


class ProductIn(Schema):
    name: str
    sku: str = ""
    kind: choices(Product.Kind) = Product.Kind.MADE
    description: str = ""
    price: Decimal = Decimal("0")
    #: What one unit costs, set by hand; null to have it calculated.
    cost_override: Decimal | None = None
    other_cost: Decimal = Decimal("0")
    batch_size: int = 1
    #: Older clients: a cost given here becomes the override.
    cost: Decimal | None = None
    unit: str = "each"
    tax_category: UUID | None = None
    is_active: bool = True


def _apply(product: Product, payload: ProductIn) -> Product:
    data = payload.dict()
    product.tax_category_id = data.pop("tax_category")
    cost = data.pop("cost")
    if cost is not None and data["cost_override"] is None:
        data["cost_override"] = cost or None
    for field, value in data.items():
        setattr(product, field, value)
    return product


@router.get("/", response=list[ProductOut])
@paginate(LimitOffsetPagination)
def list_products(request, q: str = ""):
    qs = Product.objects.all()
    if q.strip():
        qs = qs.filter(Product.search_filter(q.strip()))
    return access.scoped(request.actor, "catalog.view", qs)


@router.post("/", response={201: ProductOut})
def create_product(request, payload: ProductIn):
    access.require(request.actor, "catalog.edit")
    return Status(201, services.save_product(_apply(Product(), payload)))


@router.put("/{product_id}", response=ProductOut)
def update_product(request, product_id: UUID, payload: ProductIn):
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=product_id)
    before = audit.snapshot(product)
    return services.save_product(_apply(product, payload), before=before)


class ArchiveIn(Schema):
    archived: bool = True


@router.post("/{product_id}/archive", response=ProductOut)
def archive_product(request, product_id: UUID, payload: ArchiveIn):
    """Archive a product (kept where it is used, not offered for new quotes) or bring it back."""
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=product_id)
    return archive.set_archived(product, payload.archived)


@router.delete("/{product_id}", response={204: None})
def delete_product(request, product_id: UUID):
    """Delete a product nothing has used yet; one on quotes or invoices is refused."""
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=product_id)
    archive.delete_unused(product)
    return Status(204, None)


# ---------------------------------------------------------------------------
# Routings: the steps it takes to make a product
# ---------------------------------------------------------------------------


class RoutingStepOut(ModelSchema):
    class Meta:
        model = RoutingStep
        fields = [
            "id",
            "position",
            "name",
            "instructions",
            "machine",
            "setup_minutes",
            "machine_minutes",
            "labor_minutes",
            "wait_hours",
        ]


class RoutingStepIn(Schema):
    name: str
    instructions: str = ""
    machine: UUID | None = None
    #: Once per batch.
    setup_minutes: Decimal = Decimal("0")
    #: Per unit.
    machine_minutes: Decimal = Decimal("0")
    #: Per unit, hands-on.
    labor_minutes: Decimal = Decimal("0")
    #: Once per batch: how long the work sits (curing, drying) before the next step.
    wait_hours: Decimal = Decimal("0")


def _step(request, product_id: UUID, step_id: UUID) -> RoutingStep:
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=product_id)
    return get_object_or_404(product.routing_steps, pk=step_id)


def _fill_step(step: RoutingStep, payload: RoutingStepIn) -> RoutingStep:
    data = payload.dict()
    machine = data.pop("machine")
    step.machine = get_object_or_404(Machine, pk=machine) if machine else None
    for field, value in data.items():
        setattr(step, field, value)
    return step


@router.get("/{product_id}/routing", response=list[RoutingStepOut])
def list_routing(request, product_id: UUID):
    product = access.get_or_404(request.actor, "catalog.view", Product.objects.all(), pk=product_id)
    return product.routing_steps.all()


@router.post("/{product_id}/routing", response={201: RoutingStepOut})
def add_step(request, product_id: UUID, payload: RoutingStepIn):
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=product_id)
    return Status(201, services.save_step(_fill_step(RoutingStep(product=product), payload)))


@router.put("/{product_id}/routing/{step_id}", response=RoutingStepOut)
def update_step(request, product_id: UUID, step_id: UUID, payload: RoutingStepIn):
    return services.save_step(_fill_step(_step(request, product_id, step_id), payload))


class MoveIn(Schema):
    #: "up" or "down".
    direction: str


@router.post("/{product_id}/routing/{step_id}/move", response=list[RoutingStepOut])
def move_step(request, product_id: UUID, step_id: UUID, payload: MoveIn):
    step = _step(request, product_id, step_id)
    services.move_step(step, -1 if payload.direction == "up" else 1)
    return step.product.routing_steps.all()


@router.delete("/{product_id}/routing/{step_id}", response={204: None})
def delete_step(request, product_id: UUID, step_id: UUID):
    services.delete_step(_step(request, product_id, step_id))
    return Status(204, None)
