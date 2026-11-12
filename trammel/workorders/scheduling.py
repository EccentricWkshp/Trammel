"""The shop schedule (0.8.0): every open work order's operations laid onto the
shop's hours, its machines and the day's crew.

* **The queue.** Work orders are planned one after another: rush and high
  priority jobs first, then the earliest promised (or due) date, then the order
  they were made. Earlier work gets first call on people and machines; later
  work fits around it. Work orders waiting on materials or the customer aren't
  planned until they resume.
* **Operations** of a work order run in their order, each after the one before
  it and its wait. What's left of one is its planned time less the time logged
  against it; done and skipped ones are over, though a wait after a finished
  operation still holds up the next.
* **Hands-on time** needs a person (the day's crew is how many can work at once)
  and, for an operation on a machine, that machine. **Machine time** then needs
  only the machine. Both happen in shop hours. **Waits** run around the clock.
* Time is planned in 15-minute slots, and an operation's work can be split
  around other work. Nothing is planned beyond the horizon (half a year).

The plan is worked out when it's asked for; nothing is stored.
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import F
from django.utils import timezone

from trammel.core import workhours

from . import hooks
from .models import Operation, Status, WorkOrder

SLOT = 15  # minutes
HORIZON_DAYS = 183
ZERO = Decimal("0")
#: Work orders the schedule plans: open and in progress (not waiting).
PLANNED = (Status.OPEN, Status.IN_PROGRESS)


@dataclass
class Booking:
    """A stretch of an operation's work on the shop's wall clock."""

    kind: str  # "labor" (hands-on) or "machine"
    start: dt.datetime
    end: dt.datetime
    machine_id: object = None


@dataclass
class OpPlan:
    op: Operation
    start: dt.datetime | None = None
    finish: dt.datetime | None = None
    bookings: list[Booking] = field(default_factory=list)
    #: More work than fits in the horizon.
    beyond: bool = False


@dataclass
class WorkOrderPlan:
    wo: WorkOrder
    position: int
    ops: list[OpPlan] = field(default_factory=list)
    start: dt.datetime | None = None
    finish: dt.datetime | None = None
    beyond: bool = False

    @property
    def has_work(self) -> bool:
        return any(p.bookings for p in self.ops)

    @property
    def late(self) -> tuple[str, dt.date] | None:
        return late(self.wo, self.finish)


@dataclass
class Plan:
    hours: workhours.Hours
    now: dt.datetime  # wall clock
    work_orders: list[WorkOrderPlan]
    waiting: list[WorkOrder]
    #: Capacity: per slot, its start, end and the crew working then.
    slots: list[tuple[dt.datetime, dt.datetime, int]]

    def for_work_order(self, wo) -> WorkOrderPlan | None:
        return next((p for p in self.work_orders if p.wo.pk == wo.pk), None)

    def aware(self, wall: dt.datetime) -> dt.datetime:
        return self.hours.aware(wall)

    def bookings(self, first: dt.datetime, last: dt.datetime):
        """(work order plan, operation plan, booking) overlapping [first, last)."""
        for wp in self.work_orders:
            for op in wp.ops:
                for b in op.bookings:
                    if b.start < last and b.end > first:
                        yield wp, op, b

    def available_minutes(self, first: dt.datetime, last: dt.datetime) -> int:
        """Shop minutes in [first, last): what one machine could run."""
        return sum(_overlap(start, end, first, last) for start, end, _crew in self.slots)

    def crew_minutes(self, first: dt.datetime, last: dt.datetime) -> int:
        """Person-minutes of hands-on work the crew could do in [first, last)."""
        return sum(_overlap(start, end, first, last) * crew for start, end, crew in self.slots)


def _overlap(a0, a1, b0, b1) -> int:
    return max(0, int((min(a1, b1) - max(a0, b0)).total_seconds() // 60))


def late(wo, finish) -> tuple[str, dt.date] | None:
    """The first date a planned finish misses: (what it is, the date)."""
    if finish is None:
        return None
    day = timezone.localtime(finish).date() if timezone.is_aware(finish) else finish.date()
    job = wo.job
    for label, date in (
        ("the job's promised date", job.promised_date),
        ("the job's due date", job.due_date),
        ("the work order's target", wo.target_completion),
    ):
        if date and day > date:
            return label, date
    return None


def queue():
    """Work orders to plan, in the order they get people and machines."""
    return (
        WorkOrder.objects.filter(status__in=PLANNED)
        .select_related("job__customer")
        .order_by(
            F("job__priority").desc(),
            F("job__promised_date").asc(nulls_last=True),
            F("job__due_date").asc(nulls_last=True),
            "created_at",
            "pk",
        )
    )


def _slots(
    hours: workhours.Hours, start: dt.datetime
) -> list[tuple[dt.datetime, dt.datetime, int]]:
    """Every working 15 minutes from `start` (wall clock) to the horizon, with
    the crew working then."""
    out = []
    first = start.date() - dt.timedelta(days=1)
    for offset in range(HORIZON_DAYS + 1):
        day = first + dt.timedelta(days=offset)
        span = hours.shift_on(day)
        if span is None:
            continue
        crew = hours.crew_on(day)
        t = span[0]
        while t < span[1]:
            end = min(t + dt.timedelta(minutes=SLOT), span[1])
            if end > start:
                out.append((max(t, start), end, crew))
            t = end
    return out


class _Board:
    """Who and what is free in each slot, as the plan fills it."""

    def __init__(self, slots):
        self.slots = slots
        self.starts = [s for s, _, _ in slots]
        self.crew = [c for _, _, c in slots]
        self.machines: dict = defaultdict(lambda: bytearray(len(slots)))

    def first_from(self, t: dt.datetime) -> int:
        """The first slot ending after `t` (a slot it falls in counts)."""
        i = bisect.bisect_right(self.starts, t) - 1
        if i >= 0 and self.slots[i][1] > t:
            return i
        return max(i + 1, 0)

    def take(self, i: int, need: int, *, crew: bool, machine) -> list[int]:
        """`need` free slots from slot `i` on; [] when they don't fit."""
        got = []
        busy = self.machines[machine] if machine is not None else None
        for j in range(i, len(self.slots)):
            if crew and self.crew[j] <= 0:
                continue
            if busy is not None and busy[j]:
                continue
            got.append(j)
            if len(got) == need:
                break
        if len(got) < need:
            return []
        for j in got:
            if crew:
                self.crew[j] -= 1
            if busy is not None:
                busy[j] = 1
        return got


def _runs(board: _Board, picked: list[int], kind: str, machine) -> list[Booking]:
    """Slots taken, as stretches of time (back-to-back slots joined)."""
    out: list[Booking] = []
    for j in picked:
        start, end, _ = board.slots[j]
        if out and out[-1].end == start:
            out[-1].end = end
        else:
            out.append(Booking(kind, start, end, machine))
    return out


def _remaining(planned, logged) -> int:
    left = (planned or ZERO) - Decimal(logged or 0)
    return max(0, math.ceil(left / SLOT)) if left > 0 else 0


def build(now: dt.datetime | None = None) -> Plan:
    """The shop's plan from `now` (aware; default: now)."""
    from .services import operation_tree

    hours = workhours.Hours.load()
    wall_now = hours.local(now or timezone.now())
    slots = _slots(hours, wall_now)
    board = _Board(slots)
    plans: list[WorkOrderPlan] = []
    for position, wo in enumerate(queue(), start=1):
        plan = WorkOrderPlan(wo=wo, position=position)
        plans.append(plan)
        logged = hooks.logged_minutes(wo)
        ready = wall_now
        for op in operation_tree(wo):
            wait = dt.timedelta(hours=float(op.planned_wait_hours or 0))
            if op.status != Operation.State.PENDING:
                if op.status == Operation.State.DONE and op.completed_at and wait:
                    ready = max(ready, hours.local(op.completed_at) + wait)
                continue
            own = logged.get(op.pk, {})
            labor = _remaining(op.planned_labor_minutes, own.get("labor"))
            machine = _remaining(op.planned_machine_minutes, own.get("machine"))
            op_plan = OpPlan(op=op)
            plan.ops.append(op_plan)
            if not (labor or machine):
                op_plan.start = op_plan.finish = ready
                ready += wait
                continue
            i = board.first_from(ready)
            picked = board.take(i, labor, crew=True, machine=op.machine_id) if labor else []
            if labor and not picked:
                op_plan.beyond = plan.beyond = True
                break
            op_plan.bookings += _runs(board, picked, "labor", op.machine_id)
            after = (picked[-1] + 1) if picked else i
            if machine:
                ran = board.take(after, machine, crew=False, machine=op.machine_id or ("op", op.pk))
                if not ran:
                    op_plan.beyond = plan.beyond = True
                    break
                op_plan.bookings += _runs(board, ran, "machine", op.machine_id)
            op_plan.start = op_plan.bookings[0].start
            op_plan.finish = op_plan.bookings[-1].end
            ready = op_plan.finish + wait
        worked = [p for p in plan.ops if p.bookings]
        if worked and not plan.beyond:
            plan.start = min(p.start for p in worked)
            plan.finish = max(max(p.finish for p in worked), ready)
        elif not plan.beyond and ready > wall_now:
            plan.finish = ready  # nothing to do but a wait still running
    # Wall clock to aware datetimes, once everything is placed.
    for plan in plans:
        plan.start = hours.aware(plan.start) if plan.start else None
        plan.finish = hours.aware(plan.finish) if plan.finish else None
        for op in plan.ops:
            op.start = hours.aware(op.start) if op.start else None
            op.finish = hours.aware(op.finish) if op.finish else None
            for b in op.bookings:
                b.start, b.end = hours.aware(b.start), hours.aware(b.end)
    waiting = list(
        WorkOrder.objects.filter(status=Status.WAITING)
        .select_related("job__customer")
        .order_by("created_at")
    )
    aware_slots = [(hours.aware(s), hours.aware(e), c) for s, e, c in slots]
    return Plan(hours, hours.aware(wall_now), plans, waiting, aware_slots)
