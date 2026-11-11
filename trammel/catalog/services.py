"""Catalog services: products, their routings and their cost."""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from trammel.core import audit
from trammel.core.money import round_money
from trammel.core.services import save_audited

from . import costing
from .models import Product, RoutingStep


def _override(cost) -> Decimal | None:
    """A cost typed with a new product becomes its override; none means calculated."""
    return Decimal(str(cost)) if cost else None


@transaction.atomic
def save_product(product: Product, *, before: dict | None = None) -> Product:
    """Save a product, validated and audited, and bring its cost up to date."""
    save_audited(product, before=before)
    costing.refresh([product])
    return product


def create_product(**fields) -> Product:
    """A new catalog product, validated and audited (from a quote line, say)."""
    fields["price"] = round_money(fields.get("price") or Decimal("0"))
    if "cost" in fields:
        fields["cost_override"] = _override(fields.pop("cost"))
    return save_product(Product(**fields))


def save_adhoc(
    *,
    description: str,
    price: Decimal,
    cost: Decimal = Decimal("0"),
    unit: str = "each",
    tax_category=None,
) -> tuple[Product, bool]:
    """Keep a one-off quote or sale line as a catalog product (FR-PRD-2).

    The product is named after the line's first line of text. An active product
    with that name already is reused, not duplicated. Returns (product, created).
    """
    name = (description.strip().splitlines() or [""])[0].strip()[:200]
    existing = Product.objects.filter(name__iexact=name, is_active=True).first()
    if existing is not None:
        return existing, False
    product = save_product(
        Product(
            name=name,
            description=description.strip() if description.strip() != name else "",
            price=round_money(price),
            cost_override=_override(cost),
            unit=unit,
            tax_category=tax_category,
            kind=Product.Kind.MADE,
        )
    )
    return product, True


# ---------------------------------------------------------------------------
# Routings (Phase 2b)
# ---------------------------------------------------------------------------


@transaction.atomic
def save_step(step: RoutingStep) -> RoutingStep:
    """Add or change one step of a product's routing."""
    if step.machine_minutes and step.machine is None:
        raise ValidationError({"machine": "Choose the machine its machine time is on."})
    adding = step._state.adding
    if adding and not step.position:
        last = RoutingStep.objects.filter(product=step.product).aggregate(m=Max("position"))["m"]
        step.position = (last or 0) + 10
    step.full_clean(exclude=["product"])
    step.save()
    audit.record(
        "product.routing",
        step.product,
        summary=f"{step.product}: {'added' if adding else 'changed'} step “{step.name}”",
    )
    costing.refresh([step.product])
    return step


@transaction.atomic
def move_step(step: RoutingStep, direction: int) -> None:
    """Swap the step with the one before (-1) or after (+1) it."""
    steps = list(step.product.routing_steps.order_by("position", "id"))
    i = next(n for n, s in enumerate(steps) if s.pk == step.pk)
    j = i + direction
    if not 0 <= j < len(steps):
        return
    steps[i], steps[j] = steps[j], steps[i]
    for n, s in enumerate(steps, start=1):
        RoutingStep.objects.filter(pk=s.pk).update(position=n * 10)


@transaction.atomic
def delete_step(step: RoutingStep) -> None:
    product, name = step.product, step.name
    step.delete()
    audit.record("product.routing", product, summary=f"{product}: removed step “{name}”")
    costing.refresh([product])


def production_time(product: Product, quantity=None, *, steps=None):
    """How long making `quantity` (default: the usual batch) takes, from the
    routing: working time fitted into shop hours plus each step's wait, one step
    at a time. None if the routing has no time in it."""
    from trammel.core import workhours

    quantity = Decimal(quantity or product.batch_size or 1)
    steps = product.routing_steps.all() if steps is None else steps
    plan = [(s.planned_work(quantity), s.wait_hours) for s in steps]
    if not any(work or wait for work, wait in plan):
        return None
    return workhours.estimate(plan)
