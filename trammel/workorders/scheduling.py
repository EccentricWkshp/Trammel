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
* **Moves and pins (0.8.1).** A work order moved in the queue sits just before
  or after another one. An operation pinned to a day is planned first, from
  that day's opening (or the time it's pinned to), and everything else fits
  around it; it's flagged when the steps before it can't be done by then. One
  pinned to be finished that day also gets that day's time outside shop hours,
  until midnight, for itself alone: staying late, coming in early, or working
  a day off, without changing the shop's hours. An operation on a machine in a
  group runs on whichever machine of the group would finish it first, unless
  it's pinned to its own.

The plan is worked out when it's asked for; nothing is stored.
"""

from __future__ import annotations

import bisect
import datetime as dt
import itertools
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
    #: Outside shop hours: an operation pinned to be finished that day.
    after_hours: bool = False


@dataclass
class OpPlan:
    op: Operation
    start: dt.datetime | None = None
    finish: dt.datetime | None = None
    bookings: list[Booking] = field(default_factory=list)
    #: More work than fits in the horizon.
    beyond: bool = False
    #: The machine it's planned on: its own, or another in its group.
    machine_id: object = None
    #: Planned from its pinned day, ahead of the queue.
    pinned: bool = False
    #: Pinned before the steps ahead of it can be done.
    too_early: bool = False

    @property
    def other_machine(self) -> bool:
        """Planned on another machine of its group than the one it names."""
        return self.machine_id is not None and self.machine_id != self.op.machine_id


@dataclass
class WorkOrderPlan:
    wo: WorkOrder
    position: int
    ops: list[OpPlan] = field(default_factory=list)
    start: dt.datetime | None = None
    finish: dt.datetime | None = None
    beyond: bool = False

    @property
    def moved(self) -> bool:
        """Moved in the queue, away from where its priority and dates put it."""
        return getattr(self.wo, "queue_moved", False)

    @property
    def too_early(self) -> list[OpPlan]:
        """Operations pinned before the steps ahead of them can be done."""
        return [p for p in self.ops if p.too_early]

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


def queue() -> list[WorkOrder]:
    """Work orders to plan, in the order they get people and machines: by
    priority and dates, then each move made in the queue, oldest first. A moved
    work order has `queue_moved` set."""
    order = list(
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
    moves = sorted(
        (wo for wo in order if wo.queue_anchor_id and wo.queue_moved_at),
        key=lambda wo: (wo.queue_moved_at, wo.pk),
    )
    queued = {wo.pk for wo in order}
    for wo in moves:
        # A move next to a work order that has left the queue no longer applies.
        if wo.queue_anchor_id == wo.pk or wo.queue_anchor_id not in queued:
            continue
        order.remove(wo)
        at = next(n for n, other in enumerate(order) if other.pk == wo.queue_anchor_id)
        order.insert(at + (1 if wo.queue_side == "after" else 0), wo)
        wo.queue_moved = True
    return order


def move(wo: WorkOrder, direction: str) -> tuple[WorkOrder | None, str]:
    """Where moving `wo` "up" or "down" one place puts it: (the work order it
    goes next to, "before" or "after"); (None, "") when it can't go further."""
    order = queue()
    at = next((n for n, other in enumerate(order) if other.pk == wo.pk), None)
    if at is None:
        return None, ""
    if direction == "up":
        return (order[at - 1], "before") if at > 0 else (None, "")
    return (order[at + 1], "after") if at + 1 < len(order) else (None, "")


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


def _extra_slots(hours: workhours.Hours, windows) -> list:
    """Time outside shop hours given to operations pinned to be finished on
    their day: (start, end, crew, the operations it's for), in 15-minute steps.
    `windows` are (start, end, operation id), wall clock."""
    by_day: dict = defaultdict(list)
    for start, end, owner in windows:
        if end > start:
            by_day[start.date()].append((start, end, owner))
    out = []
    for day, spans in by_day.items():
        shift = hours.shift_on(day)
        crew = max(hours.crew_on(day), 1)
        marks = {t for s, e, _ in spans for t in (s, e)}
        t = dt.datetime.combine(day, dt.time(0))
        last = max(e for _, e, _ in spans)
        while t < last:
            marks.add(t)
            t += dt.timedelta(minutes=SLOT)
        if shift:
            marks |= set(shift)
        marks = sorted(m for m in marks if min(s for s, _, _ in spans) <= m <= last)
        for a, b in itertools.pairwise(marks):
            if shift and a < shift[1] and b > shift[0]:
                continue  # shop hours: the regular slots
            owners = frozenset(o for s, e, o in spans if s <= a and b <= e)
            if owners:
                out.append((a, b, crew, owners))
    return out


class _Board:
    """Who and what is free in each slot, as the plan fills it. Slots outside
    shop hours belong to the operations pinned to them."""

    def __init__(self, slots, extra=()):
        merged = sorted(
            [(s, e, c, None) for s, e, c in slots] + list(extra), key=lambda x: (x[0], x[1])
        )
        self.slots = [(s, e, c) for s, e, c, _ in merged]
        self.owners = [o for _, _, _, o in merged]
        self.starts = [s for s, _, _ in self.slots]
        self.crew = [c for _, _, c in self.slots]
        self.machines: dict = defaultdict(lambda: bytearray(len(self.slots)))

    def first_from(self, t: dt.datetime) -> int:
        """The first slot ending after `t` (a slot it falls in counts)."""
        i = bisect.bisect_right(self.starts, t) - 1
        if i >= 0 and self.slots[i][1] > t:
            return i
        return max(i + 1, 0)

    def find(self, i: int, need: int, *, crew: bool, machine, owner=None) -> list[int]:
        """`need` free slots from slot `i` on, without taking them; [] when
        they don't fit. Slots outside shop hours only for their `owner`."""
        got = []
        busy = self.machines[machine] if machine is not None else None
        for j in range(i, len(self.slots)):
            if self.owners[j] is not None and owner not in self.owners[j]:
                continue
            if crew and self.crew[j] <= 0:
                continue
            if busy is not None and busy[j]:
                continue
            got.append(j)
            if len(got) == need:
                break
        return got if len(got) == need else []

    def commit(self, got: list[int], *, crew: bool, machine) -> None:
        busy = self.machines[machine] if machine is not None else None
        for j in got:
            if crew:
                self.crew[j] -= 1
            if busy is not None:
                busy[j] = 1


def _runs(board: _Board, picked: list[int], kind: str, machine) -> list[Booking]:
    """Slots taken, as stretches of time (back-to-back slots joined)."""
    out: list[Booking] = []
    for j in picked:
        start, end, _ = board.slots[j]
        extra = board.owners[j] is not None
        if out and out[-1].end == start and out[-1].after_hours == extra:
            out[-1].end = end
        else:
            out.append(Booking(kind, start, end, machine, after_hours=extra))
    return out


def _remaining(planned, logged) -> int:
    left = (planned or ZERO) - Decimal(logged or 0)
    return max(0, math.ceil(left / SLOT)) if left > 0 else 0


class _Machines:
    """Which machines can stand in for an operation's: the active machines of
    its group, its own first."""

    def __init__(self):
        from trammel.shop.models import Machine

        self.group: dict = {}
        self.members: dict = defaultdict(list)
        for m in Machine.objects.order_by("name"):
            key = m.group.strip().casefold()
            self.group[m.pk] = key
            if key and m.is_active:
                self.members[key].append(m.pk)

    def for_op(self, op: Operation) -> list:
        own = op.machine_id
        if own is None:
            return [None]
        key = self.group.get(own, "")
        if op.machine_pinned or not key:
            return [own]
        return [own] + [m for m in self.members[key] if m != own]


def _place(board: _Board, op: Operation, i: int, labor: int, machine: int, candidates, owner=None):
    """Books an operation's hands-on then machine time from slot `i` on the
    candidate machine that finishes it first: (that machine, its bookings), or
    None when it doesn't fit the horizon."""
    best = None
    for m in candidates:
        run_key = m if m is not None else ("op", op.pk)
        picked = board.find(i, labor, crew=True, machine=m, owner=owner) if labor else []
        if labor and not picked:
            continue
        after = (picked[-1] + 1) if picked else i
        ran = (
            board.find(after, machine, crew=False, machine=run_key, owner=owner) if machine else []
        )
        if machine and not ran:
            continue
        end = (ran or picked)[-1]
        if best is None or end < best[0]:
            best = (end, m, run_key, picked, ran)
    if best is None:
        return None
    _end, m, run_key, picked, ran = best
    board.commit(picked, crew=True, machine=m)
    board.commit(ran, crew=False, machine=run_key)
    return m, _runs(board, picked, "labor", m) + _runs(board, ran, "machine", m)


def _work(op: Operation, logged: dict) -> tuple[int, int]:
    """Slots of hands-on and machine time still to do on an operation."""
    own = logged.get(op.pk, {})
    return (
        _remaining(op.planned_labor_minutes, own.get("labor")),
        _remaining(op.planned_machine_minutes, own.get("machine")),
    )


def _usual_opening(hours: workhours.Hours, day: dt.date) -> dt.time:
    """When the shop usually opens: the next working day's opening."""
    for k in range(1, 8):
        span = hours.shift_on(day + dt.timedelta(days=k))
        if span:
            return span[0].time()
    return dt.time(8)


def _pin_window(hours: workhours.Hours, op: Operation, now: dt.datetime):
    """When a pinned operation starts (wall clock), and for one to be finished
    that day, until when it may work outside shop hours (else None)."""
    day = op.pinned_date
    span = hours.shift_on(day)
    at = dt.datetime.combine(day, op.pinned_from) if op.pinned_from else None
    if op.pinned_overtime:
        start = at or (span[0] if span else dt.datetime.combine(day, _usual_opening(hours, day)))
        return max(start, now), dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0))
    opening = span[0] if span else dt.datetime.combine(day, dt.time(0))
    return max(max(opening, at) if at else opening, now), None


def build(now: dt.datetime | None = None) -> Plan:
    """The shop's plan from `now` (aware; default: now)."""
    from .services import operation_tree

    hours = workhours.Hours.load()
    wall_now = hours.local(now or timezone.now())
    slots = _slots(hours, wall_now)
    machines = _Machines()
    order = queue()
    trees = {wo.pk: (operation_tree(wo), hooks.logged_minutes(wo)) for wo in order}

    # Operations pinned to a day get first call on it, in queue order; one to
    # be finished that day gets the day's time outside shop hours too.
    to_pin = []
    for wo in order:
        ops, logged = trees[wo.pk]
        for op in ops:
            if op.status != Operation.State.PENDING or op.pinned_date is None:
                continue
            labor, machine = _work(op, logged)
            if labor or machine:
                to_pin.append((op, labor, machine, *_pin_window(hours, op, wall_now)))
    board = _Board(
        slots,
        _extra_slots(hours, [(start, until, op.pk) for op, _, _, start, until in to_pin if until]),
    )
    pinned: dict = {}
    for op, labor, machine, start, until in to_pin:
        pinned[op.pk] = _place(
            board,
            op,
            board.first_from(start),
            labor,
            machine,
            machines.for_op(op),
            owner=op.pk if until else None,
        )

    plans: list[WorkOrderPlan] = []
    for position, wo in enumerate(order, start=1):
        plan = WorkOrderPlan(wo=wo, position=position)
        plans.append(plan)
        ops, logged = trees[wo.pk]
        ready = wall_now
        for op in ops:
            wait = dt.timedelta(hours=float(op.planned_wait_hours or 0))
            if op.status != Operation.State.PENDING:
                if op.status == Operation.State.DONE and op.completed_at and wait:
                    ready = max(ready, hours.local(op.completed_at) + wait)
                continue
            labor, machine = _work(op, logged)
            op_plan = OpPlan(op=op)
            plan.ops.append(op_plan)
            if not (labor or machine):
                op_plan.start = op_plan.finish = ready
                ready += wait
                continue
            if op.pk in pinned:
                placed = pinned[op.pk]
                op_plan.pinned = True
            else:
                placed = _place(
                    board, op, board.first_from(ready), labor, machine, machines.for_op(op)
                )
            if placed is None:
                op_plan.beyond = plan.beyond = True
                break
            op_plan.machine_id, op_plan.bookings = placed
            op_plan.start = op_plan.bookings[0].start
            op_plan.finish = op_plan.bookings[-1].end
            if op_plan.pinned and op_plan.start < ready:
                op_plan.too_early = True
            ready = max(ready, op_plan.finish) + wait
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
