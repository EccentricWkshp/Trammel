"""What a product costs to make: put together from parts.

A product's cost is calculated from its parts unless it has an override:

* its routing (here): hands-on labor, setup spread over the usual batch, and
  machine time, at the rates a quote's labor and machine lines cost at;
* its bill of materials, which the stock module contributes;
* its other cost per unit, typed on the product.

Modules contribute with ``@costing.part``; a module never needs to know who
else does. ``refresh`` stores the result on ``Product.cost``, which quotes,
invoices and reports read, whenever something it depends on changes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

from trammel.core import audit
from trammel.core.money import format_money

ZERO = Decimal("0")
Q4 = Decimal("0.0001")
SIXTY = Decimal("60")


@dataclass(frozen=True)
class Part:
    label: str
    amount: Decimal  # per unit
    detail: str = ""


_parts: list[Callable] = []


def part(fn: Callable) -> Callable:
    """Register `fn(product) -> [Part, ...]`."""
    _parts.append(fn)
    return fn


def _enabled() -> list[Callable]:
    from trammel.core import modules

    on = modules.enabled_keys()
    return [f for f in _parts if (key := modules.module_of(f.__module__)) is None or key in on]


def parts(product) -> list[Part]:
    """Everything one unit is made of, other cost last."""
    out = [p for fn in _enabled() for p in fn(product)]
    if product.other_cost:
        out.append(Part("Other", product.other_cost))
    return out


def calculated(product, found: list[Part] | None = None) -> Decimal:
    found = parts(product) if found is None else found
    return sum((p.amount for p in found), ZERO).quantize(Q4)


def effective(product) -> Decimal:
    """The override when there is one, otherwise the calculated cost."""
    return product.cost_override if product.cost_override is not None else calculated(product)


def refresh(products: Iterable) -> int:
    """Store each product's effective cost where it has changed. Returns how many changed."""
    from .models import Product

    changed = 0
    for product in products:
        cost = effective(product)
        if cost != product.cost:
            before = product.cost
            Product.objects.filter(pk=product.pk).update(cost=cost)
            product.cost = cost
            audit.record(
                "product.cost",
                product,
                summary=f"{product}: cost {before.normalize():f} → {cost.normalize():f}",
            )
            changed += 1
    return changed


def refresh_all() -> int:
    from .models import Product

    return refresh(Product.objects.all())


# ---------------------------------------------------------------------------
# The routing's part
# ---------------------------------------------------------------------------


def labor_rate() -> Decimal:
    """An hour of hands-on work at cost: the shop's labor cost (or, if that is
    blank, its labor rate) plus overhead."""
    from trammel.core.models import ShopRates

    rates = ShopRates.load()
    return rates.labor_cost + rates.shop_overhead_rate


def machine_rate(machine) -> Decimal:
    """An hour of the machine, as a quote's machine line costs it."""
    from trammel.core.models import ShopRates

    return machine.cost_rate + ShopRates.load().shop_overhead_rate


def step_costs(step, batch: int, labor: Decimal | None = None) -> dict:
    """One step's cost for one unit: labor, setup (spread over `batch`) and machine."""
    labor = labor_rate() if labor is None else labor
    machine = step.machine_minutes / SIXTY * machine_rate(step.machine) if step.machine else ZERO
    return {
        "labor": step.labor_minutes / SIXTY * labor,
        "setup": step.setup_minutes / max(batch, 1) / SIXTY * labor,
        "machine": machine,
    }


@part
def routing(product) -> list[Part]:
    steps = list(product.routing_steps.select_related("machine"))
    if not steps:
        return []
    labor = labor_rate()
    totals = {"labor": ZERO, "setup": ZERO, "machine": ZERO}
    minutes = {"labor": ZERO, "setup": ZERO, "machine": ZERO}
    for step in steps:
        for key, amount in step_costs(step, product.batch_size, labor).items():
            totals[key] += amount
        minutes["labor"] += step.labor_minutes
        minutes["setup"] += step.setup_minutes
        minutes["machine"] += step.machine_minutes
    out = []
    per_hour = f"at {format_money(labor)} an hour"
    if totals["labor"]:
        out.append(
            Part("Labor", totals["labor"].quantize(Q4), f"{_min(minutes['labor'])} each {per_hour}")
        )
    if totals["setup"]:
        out.append(
            Part(
                "Setup",
                totals["setup"].quantize(Q4),
                f"{_min(minutes['setup'])} per batch of {product.batch_size} {per_hour}",
            )
        )
    if totals["machine"]:
        out.append(
            Part("Machine time", totals["machine"].quantize(Q4), f"{_min(minutes['machine'])} each")
        )
    return out


def _min(value: Decimal) -> str:
    return f"{value.normalize():f} min"
