"""The Schedule page (0.8.0): a week of the shop plan as a timeline, a row per
machine and one for hands-on work, with each machine's load, the same week as a
list, and the queue of work orders with when each should start and finish."""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.http import content_disposition_header

from trammel.core import access, templating
from trammel.core.web import document_response, document_view, permission_required
from trammel.jobs.models import Priority
from trammel.shop.models import Machine

from . import scheduling, sheet

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


def _after_hours(plan, days) -> dict:
    """Each day's work outside shop hours (operations pinned to be finished
    that day): the earliest start and latest end, wall clock."""
    out: dict = {}
    for _wp, _op, booking in plan.bookings(
        plan.hours.aware(dt.datetime.combine(days[0], dt.time(0))),
        plan.hours.aware(dt.datetime.combine(days[-1] + dt.timedelta(days=1), dt.time(0))),
    ):
        if not booking.after_hours:
            continue
        start, end = plan.hours.local(booking.start), plan.hours.local(booking.end)
        first, last = out.get(start.date(), (start, end))
        out[start.date()] = (min(first, start), max(last, end))
    return out


def _columns(plan, days):
    """Each day's column: its x, width, the stretch of the day it draws (its
    shift, widened for work outside shop hours), and the shift itself."""
    extra = _after_hours(plan, days)
    shifts = [plan.hours.shift_on(d) for d in days]
    weights = [1.0 if (s or d in extra) else OFF_WEIGHT for d, s in zip(days, shifts, strict=True)]
    unit = (WIDTH - LABELS) / sum(weights)
    x = LABELS
    out = []
    for day, weight, shift in zip(days, weights, shifts, strict=True):
        span = shift
        if day in extra:
            first, last = extra[day]
            span = (min(first, shift[0]), max(last, shift[1])) if shift else (first, last)
        w = unit * weight
        col = {
            "day": day,
            "x": round(x, 1),
            "w": round(w, 1),
            "mid": round(x + w / 2, 1),
            "span": span,
            "shift": shift,
            "off": shift is None,
            "shaded": [],  # the stretches outside shop hours: (x, width)
        }
        if span and span != shift:
            total = (span[1] - span[0]).total_seconds()

            def at(t, x=x, w=w, span=span, total=total):
                return x + w * (t - span[0]).total_seconds() / total

            if shift is None:
                col["shaded"].append((round(x, 1), round(w, 1)))
            else:
                if span[0] < shift[0]:
                    col["shaded"].append((round(x, 1), round(at(shift[0]) - x, 1)))
                if span[1] > shift[1]:
                    col["shaded"].append((round(at(shift[1]), 1), round(x + w - at(shift[1]), 1)))
        out.append(col)
        x += w
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
            runs.append(
                {
                    "wo": item["wo"],
                    "x": item["x"],
                    "y": item["y"],
                    "w": item["w"],
                    "priority": item["priority"],
                }
            )
    labels = []
    for run in runs:
        text = short_number(run["wo"].number)
        if run["w"] >= CHAR * len(text) + 4:
            labels.append(
                {
                    "text": text,
                    # Low priority's pale blue takes dark text.
                    "light": run["priority"] == 0,
                    "x": round(run["x"] + run["w"] / 2, 1),
                    "y": run["y"] + (LANE - 6) / 2 + 4,
                }
            )
    return labels


def _overlap(a0, a1, b0, b1) -> int:
    """Minutes [a0, a1) and [b0, b1) share."""
    return max(0, int((min(a1, b1) - max(a0, b0)).total_seconds() // 60))


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
    week = hours.aware(dt.datetime.combine(first, dt.time(0)))
    booked: dict = {"labor": 0}  # minutes this week, by row
    for wp, op, booking in plan.bookings(begin, end):
        row = None
        if booking.machine_id is not None and not isinstance(booking.machine_id, tuple):
            row = booking.machine_id
            if row not in machines:
                machines[row] = op.op.machine
                rows[row] = []
        item = {
            "wo": wp.wo,
            "op": op.op,
            "priority": wp.wo.job.priority,
            "late": bool(wp.late),
            "kind": booking.kind,
            "machine": machines.get(booking.machine_id) or op.op.machine,
            "instead_of": op.op.machine if op.other_machine else None,
            "pinned": op.op.pinned_date if op.pinned else None,
            "after_hours": booking.after_hours,
            "unattended": booking.unattended,
            "from": booking.start,
            "to": booking.end,
        }
        minutes = _overlap(booking.start, booking.end, week, end)
        if booking.kind == "labor":
            booked["labor"] += minutes
        if row is not None:
            booked[row] = booked.get(row, 0) + minutes
        # A piece on each day it covers (an unattended run goes on overnight).
        for col in columns:
            if not col["span"]:
                continue
            s0, s1 = hours.aware(col["span"][0]), hours.aware(col["span"][1])
            start, stop = max(booking.start, s0), min(booking.end, s1)
            if stop <= start:
                continue
            share = col["w"] / (s1 - s0).total_seconds()
            piece = item | {
                "start": start,
                "end": stop,
                "x": round(col["x"] + (start - s0).total_seconds() * share, 1),
                "w": round(max(2.0, (stop - start).total_seconds() * share), 1),
            }
            shown.setdefault(wp.position, wp)
            if booking.kind == "labor":
                rows["labor"].append(dict(piece))
            if row is not None:
                rows[row].append(dict(piece))
        start = max(booking.start, begin)
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
                    "instead_of": item["instead_of"],
                    "pinned": item["pinned"],
                    "too_early": op.too_early,
                    "after_hours": booking.after_hours,
                    "unattended": booking.unattended,
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

    # Load: booked against what's available this week; an unattended
    # machine has the whole clock from now.
    available = plan.available_minutes(week, end)
    loads = []
    for key in rows:
        if key == "labor":
            room = plan.crew_minutes(week, end)
        elif getattr(machines[key], "runs_unattended", False):
            room = _overlap(plan.now, end, week, end)
        else:
            room = available
        used = booked.get(key, 0)
        loads.append(
            {
                "name": "Hands-on work (the crew)" if key == "labor" else str(machines[key]),
                "booked": used,
                "available": room,
                "percent": round(100 * used / room) if room else 0,
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
            "can_move": access.has_perm(request.actor, "workorders.edit"),
            "resources": sheet.resources(),
            "priorities": sorted(Priority, reverse=True),
            "key": [
                {"wp": wp, "priority": wp.wo.job.priority, "short": short_number(wp.wo.number)}
                for pos, wp in sorted(shown.items())
            ],
        },
    )


@permission_required("workorders.view")
@document_view
def schedule_sheet(request):
    """The printed shop schedule (0.8.3): each day's work in the order it's
    done, to take into the shop; the traveler's when to its what. ?from= the
    first day, ?days= how many (to 14), ?for= hands-on work ("labor") or a
    machine's id, ?per_page=1 a page a day; ?print=1 or ?format=pdf."""
    plan = scheduling.build()
    today = timezone.localdate()
    first = sheet.first_day(request.GET.get("from"), today)
    count = sheet.day_count(request.GET.get("days"), 7)
    resource = request.GET.get("for", "")
    if resource not in dict(sheet.resources()):
        resource = ""
    days = sheet.days(plan, first, count, resource)
    try:
        floor = settings.BASE_URL + reverse("floor:schedule")
    except NoReverseMatch:  # the shop-floor app is off
        floor = settings.BASE_URL + reverse("workorders:schedule")
    from trammel.core.qr import svg

    html = render_to_string(
        "workorders/schedule_sheet.html",
        {
            "days": days,
            "first": first,
            "last": first + dt.timedelta(days=count - 1),
            "resource": resource,
            "for_name": sheet.resource_name(resource),
            "per_page": request.GET.get("per_page") == "1",
            "queue": plan.work_orders,
            "waiting": plan.waiting,
            "printed_at": timezone.now(),
            "qr": svg(floor),
            "blank_rows": range(4),
            "business": request.business,
        },
        request=request,
    )
    if request.GET.get("format") == "pdf":
        try:
            data = templating.render_pdf(html)
        except templating.PdfUnavailable as exc:
            messages.error(request, str(exc))
            return redirect("workorders:schedule")
        response = HttpResponse(data, content_type="application/pdf")
        response["Content-Disposition"] = content_disposition_header(
            True, f"schedule-{first.isoformat()}.pdf"
        )
        return response
    return document_response(request, html)
