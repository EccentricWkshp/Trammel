"""The shop schedule as a sequence of work, day by day (0.8.3): what the printed
schedule sheet and the shop-floor Schedule page show. The traveler says what to
do on a work order; this says when, and in what order, across the shop."""

from __future__ import annotations

import datetime as dt

from trammel.shop.models import Machine

MAX_DAYS = 14


def resources() -> list[tuple[str, str]]:
    """What a sheet can be for: everything, hands-on work, or one machine."""
    return [("", "Everything"), ("labor", "Hands-on work")] + [
        (str(m.pk), m.name) for m in Machine.objects.filter(is_active=True).order_by("name")
    ]


def resource_name(resource: str) -> str:
    return dict(resources()).get(resource, "Everything")


def days(plan, first: dt.date, count: int, resource: str = "") -> list[dict]:
    """Each day from `first` for `count` days with work or shop hours: its
    shift, and its work in the order it starts. `resource` narrows it to
    hands-on work ("labor") or one machine (its id). Work running on from the
    day before (an unattended run overnight) opens the first day."""
    count = max(1, min(count, MAX_DAYS))
    hours = plan.hours
    span = [first + dt.timedelta(days=n) for n in range(count)]
    begin = hours.aware(dt.datetime.combine(first, dt.time(0)))
    end = hours.aware(dt.datetime.combine(span[-1] + dt.timedelta(days=1), dt.time(0)))
    names = {m.pk: m for m in Machine.objects.all()}
    by_day: dict = {d: [] for d in span}
    for wp, op, booking in plan.bookings(begin, end):
        machine_id = None if isinstance(booking.machine_id, tuple) else booking.machine_id
        if resource == "labor" and booking.kind != "labor":
            continue
        if resource not in ("", "labor") and str(machine_id) != resource:
            continue
        day = max(hours.local(booking.start).date(), first)
        by_day[day].append(
            {
                "start": booking.start,
                "end": booking.end,
                "continued": booking.start < begin,
                "overnight": hours.local(booking.end).date() > day,
                "kind": booking.kind,
                "machine": names.get(machine_id),
                "instead_of": op.op.machine if op.other_machine else None,
                "wo": wp.wo,
                "position": wp.position,
                "op": op.op,
                "minutes": int((booking.end - booking.start).total_seconds() // 60),
                "late": wp.late,
                "pinned": op.op.pinned_date if op.pinned else None,
                "too_early": op.too_early,
                "after_hours": booking.after_hours,
                "unattended": booking.unattended,
            }
        )
    out = []
    for day in span:
        items = sorted(by_day[day], key=lambda i: (i["start"], i["position"], i["kind"]))
        shift = hours.shift_on(day)
        if items or shift:
            out.append({"day": day, "shift": shift, "items": items, "crew": hours.crew_on(day)})
    return out


def first_day(value: str | None, today: dt.date) -> dt.date:
    try:
        return dt.date.fromisoformat(value or "")
    except ValueError:
        return today


def day_count(value: str | None, default: int) -> int:
    try:
        return max(1, min(int(value or default), MAX_DAYS))
    except ValueError:
        return default
