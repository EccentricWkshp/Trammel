"""How long a quote's work takes (0.8.2): each line's hands-on time, machine time
and waits, from the same sources a work order made from it plans from.

* A catalog product with a routing: its steps, for the line's quantity (setup
  once, the rest per unit), each with its machine and wait.
* Labor: the line's hours, hands-on. Machine time: the line's hours on its machine.
* An estimate: the labor and machine hours in its breakdown.
* Items, materials, resold products, credits and the rush fee take no
  production time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from trammel.shop.models import Machine

from .models import QuoteLine

HOUR = Decimal("60")
ZERO = Decimal("0")
#: Breakdown rows made before 0.8.2 give their hours only in the detail: "2.5 h × 75/h".
_HOURS = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*h\b")


@dataclass
class Step:
    name: str
    machine: Machine | None = None
    labor: Decimal = ZERO  # hands-on minutes
    run: Decimal = ZERO  # machine minutes
    wait: Decimal = ZERO  # hours after it

    @property
    def has_time(self) -> bool:
        return bool(self.labor or self.run or self.wait)


@dataclass
class LineTime:
    line: QuoteLine
    steps: list[Step] = field(default_factory=list)
    #: A product's routing: the steps become sub-operations on the work order.
    routed: bool = False

    @property
    def labor(self) -> Decimal:
        return sum((s.labor for s in self.steps), ZERO)

    @property
    def run(self) -> Decimal:
        return sum((s.run for s in self.steps), ZERO)

    @property
    def wait(self) -> Decimal:
        return sum((s.wait for s in self.steps), ZERO)

    @property
    def has_time(self) -> bool:
        return any(s.has_time for s in self.steps)


def _row_hours(row: dict) -> Decimal:
    if row.get("hours"):
        return Decimal(row["hours"])
    found = _HOURS.match(row.get("detail") or "")
    return Decimal(found.group(1)) if found else ZERO


def line_machine(line: QuoteLine) -> Machine | None:
    """The machine a line names, or its estimate's inputs do."""
    if line.machine_id:
        return line.machine
    pk = (line.inputs or {}).get("machine")
    return Machine.objects.filter(pk=pk).first() if pk else None


def for_line(line: QuoteLine) -> LineTime | None:
    """A line's production time; None for a line that isn't work to do (a
    credit or a resold product)."""
    if line.line_total < 0 or line.kind == QuoteLine.Kind.RUSH:
        return None
    quantity = line.quantity or ZERO
    if line.product_id:
        product = line.product
        if product.kind == "resold":
            return None
        steps = [
            Step(
                s.name,
                s.machine,
                s.planned_labor(quantity) or ZERO,
                s.planned_machine(quantity) or ZERO,
                s.wait_hours or ZERO,
            )
            for s in product.routing_steps.select_related("machine")
        ]
        return LineTime(line, steps, routed=bool(steps))
    if line.kind == QuoteLine.Kind.LABOR:
        return LineTime(line, [Step(line.description, labor=quantity * HOUR)])
    if line.kind == QuoteLine.Kind.MACHINE:
        return LineTime(line, [Step(line.description, line.machine, run=quantity * HOUR)])
    if line.kind == QuoteLine.Kind.ESTIMATE:
        rows = line.breakdown or []
        labor = sum((_row_hours(r) for r in rows if r.get("category") == "labor"), ZERO)
        run = sum((_row_hours(r) for r in rows if r.get("category") == "machine"), ZERO)
        machine = line_machine(line)
        if machine is None:
            pk = next((r["machine"] for r in rows if r.get("machine")), None)
            machine = Machine.objects.filter(pk=pk).first() if pk else None
        return LineTime(line, [Step(line.description, machine, labor * HOUR, run * HOUR)])
    return LineTime(line)  # an item or a material: nothing to time


def for_revision(revision) -> list[LineTime]:
    """Every included line's production time, in order (lines that aren't work
    left out)."""
    lines = revision.included_lines().select_related("product", "machine").order_by("position")
    return [t for line in lines if (t := for_line(line)) is not None]
