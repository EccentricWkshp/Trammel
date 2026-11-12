"""The Schedule page (0.8.0): a week of the shop plan as a timeline, a row per
machine and one for hands-on work, with each machine's load, the same week as a
list, and the queue of work orders with when each should start and finish."""

from __future__ import annotations

import datetime as dt

from django.shortcuts import render
from django.utils import timezone

from trammel.core.web import permission_required
from trammel.jobs.models import Priority
from trammel.shop.models import Machine

from . import scheduling

WIDTH = 1000  # the timeline's drawing width
LABELS = 170  # the row names on the left
HEADER = 38
LANE = 26
GAP = 6
OFF_WEIGHT = 0.3  # a day off is drawn narrow
CHAR = 7  # about how wide a digit of a bar's label is


def _week_start(request, today: dt.date) -> dt.date:
    try:
        day = dt.date.fromisoformat(request.GET.get("week", ""))
    except ValueError:
        day = today
    first = request.business.week_start
    return day - dt.timedelta(days=(day.weekday() - first) % 7)


def _columns(plan, days):
    """Each day's column: its x, width and shift (wall clock), or none if off."""
    weights = [1.0 if plan.hours.shift_on(d) else OFF_WEIGHT for d in days]
    unit = (WIDTH - LABELS) / sum(weights)
    x = LABELS
    out = []
    for day, weight in zip(days, weights, strict=True):
        span = plan.hours.shift_on(day)
        out.append(
            {
                "day": day,
                "x": round(x, 1),
                "w": round(unit * weight, 1),
                "mid": round(x + unit * weight / 2, 1),
                "span": span,
                "off": span is None,
            }
        )
        x += unit * weight
    return out


def _x(columns, hours, moment) -> tuple[dict, float] | None:
    """Where a moment falls on the timeline: (its day column, x)."""
    wall = hours.local(moment)
    for col in columns:
        span = col["span"]
        if span and span[0] <= wall <= span[1]:
            share = (wall - span[0]) / (span[1] - span[0])
            return col, col["x"] + share * col["w"]
    return None


def short_number(number: str) -> str:
    """A work order's number as a bar's label: its last part, without leading
    zeros (WO-2026-0007 is 7), so it fits short bars."""
    return number.rsplit("-", 1)[-1].lstrip("0") or "0"


def _labels(items):
    """One label for each run of a work order's bars that touch in a lane (an
    operation's hands-on time then its machine time, or work across a day
    line), centered on the run when it's wide enough."""
    runs: list[dict] = []
    for item in sorted(items, key=lambda i: (i["y"], i["x"])):
        last = runs[-1] if runs else None
        if (
            last
            and last["wo"] == item["wo"]
            and last["y"] == item["y"]
            and item["x"] <= last["x"] + last["w"] + 1
        ):
            last["w"] = max(last["x"] + last["w"], item["x"] + item["w"]) - last["x"]
        else:
            runs.append({"wo": item["wo"], "x": item["x"], "y": item["y"], "w": item["w"]})
    labels = []
    for run in runs:
        text = short_number(run["wo"].number)
        if run["w"] >= CHAR * len(text) + 4:
            labels.append(
                {
                    "text": text,
                    "x": round(run["x"] + run["w"] / 2, 1),
                    "y": run["y"] + (LANE - 6) / 2 + 4,
                }
            )
    return labels


def _lanes(items):
    """Side-by-side bookings in lanes: each gets the first lane free at its start."""
    ends: list = []
    for item in sorted(items, key=lambda i: i["start"]):
        lane = next((n for n, end in enumerate(ends) if end <= item["start"]), None)
        if lane is None:
            lane = len(ends)
            ends.append(item["end"])
        else:
            ends[lane] = item["end"]
        item["lane"] = lane
    return max(1, len(ends))


@permission_required("workorders.view")
def schedule(request):
    plan = scheduling.build()
    hours = plan.hours
    today = timezone.localdate()
    first = _week_start(request, today)
    days = [first + dt.timedelta(days=i) for i in range(7)]
    columns = _columns(plan, days)
    begin = hours.aware(dt.datetime.combine(first - dt.timedelta(days=1), dt.time(0)))
    end = hours.aware(dt.datetime.combine(first + dt.timedelta(days=7), dt.time(0)))

    machines = {m.pk: m for m in Machine.objects.filter(is_active=True).order_by("name")}
    rows: dict = {"labor": []}
    for m in machines:
        rows[m] = []
    listed = []
    shown: dict = {}  # the work orders drawn this week, by queue position
    for wp, op, booking in plan.bookings(begin, end):
        start, stop = max(booking.start, begin), min(booking.end, end)
        placed = _x(columns, hours, start)
        if placed is None:
            continue
        col, x0 = placed
        span_end = hours.aware(col["span"][1])
        x1 = _x(columns, hours, min(stop, span_end))
        width = max(2.0, (x1[1] if x1 else col["x"] + col["w"]) - x0)
        item = {
            "start": start,
            "end": stop,
            "x": round(x0, 1),
            "w": round(width, 1),
            "wo": wp.wo,
            "op": op.op,
            "priority": wp.wo.job.priority,
            "late": bool(wp.late),
            "kind": booking.kind,
            "machine": machines.get(booking.machine_id) or op.op.machine,
        }
        shown.setdefault(wp.position, wp)
        if booking.kind == "labor":
            rows["labor"].append(dict(item))
        if booking.machine_id is not None and not isinstance(booking.machine_id, tuple):
            if booking.machine_id not in machines:
                machines[booking.machine_id] = op.op.machine
                rows[booking.machine_id] = []
            rows[booking.machine_id].append(dict(item))
        if hours.local(start).date() in days:
            listed.append(
                {
                    "day": hours.local(booking.start).date(),
                    "start": booking.start,
                    "end": booking.end,
                    "kind": booking.kind,
                    "machine": machines.get(booking.machine_id)
                    if not isinstance(booking.machine_id, tuple)
                    else None,
                    "wo": wp.wo,
                    "op": op.op,
                    "late": bool(wp.late),
                }
            )

    # The drawing: rows top to bottom, each as tall as its lanes.
    crew_today = max((hours.crew_on(d) for d in days), default=1)
    drawn = []
    y = HEADER
    for key, items in rows.items():
        lanes = _lanes(items) if key == "labor" else 1
        if key == "labor":
            lanes = max(lanes, crew_today or 1)
            name = "Hands-on work"
        else:
            name = str(machines[key])
        height = lanes * LANE
        for item in items:
            lane = item.get("lane", 0) if key == "labor" else 0
            item["y"] = y + lane * LANE + 3
            item["h"] = LANE - 6
        drawn.append(
            {
                "name": name,
                "y": y,
                "h": height,
                "label_y": y + height / 2 + 4,
                "items": items,
                "labels": _labels(items),
            }
        )
        y += height + GAP
    height = y

    # Load: booked against what's available this week.
    available = plan.available_minutes(begin, end)
    loads = []
    for key, items in rows.items():
        booked = sum(int((i["end"] - i["start"]).total_seconds() // 60) for i in items)
        room = plan.crew_minutes(begin, end) if key == "labor" else available
        loads.append(
            {
                "name": "Hands-on work (the crew)" if key == "labor" else str(machines[key]),
                "booked": booked,
                "available": room,
                "percent": round(100 * booked / room) if room else 0,
            }
        )

    listed.sort(key=lambda i: (i["start"], i["kind"]))
    by_day = [
        {"day": d, "items": [i for i in listed if i["day"] == d], "span": hours.shift_on(d)}
        for d in days
    ]
    return render(
        request,
        "workorders/schedule.html",
        {
            "plan": plan,
            "columns": columns,
            "rows": drawn,
            "width": WIDTH,
            "height": height,
            "labels": LABELS,
            "loads": loads,
            "by_day": by_day,
            "first": first,
            "last": days[-1],
            "previous": (first - dt.timedelta(days=7)).isoformat(),
            "next": (first + dt.timedelta(days=7)).isoformat(),
            "this_week": first <= today <= days[-1],
            "queue": plan.work_orders,
            "priorities": sorted(Priority, reverse=True),
            "key": [
                {"wp": wp, "priority": wp.wo.job.priority, "short": short_number(wp.wo.number)}
                for pos, wp in sorted(shown.items())
            ],
        },
    )
