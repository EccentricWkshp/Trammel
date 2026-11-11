"""The shop's working hours, for estimating when work will be done.

Each day of the week the shop works has its own hours (a shift). A shift that
ends at or before it starts runs past midnight, as a night shift, and belongs to
the day it starts; the same start and end time is around the clock.

Work (hands-on and machine time) only happens in those hours. Waiting (glue
clamping, finish curing, paint drying) goes on around the clock, nights and days
off included. Steps are taken one at a time, in order: each step's work, then its
wait, before the next one starts.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
DAY = 24 * 60

Shift = tuple[dt.time, dt.time]


def shift_minutes(start: dt.time, end: dt.time) -> int:
    """How long a shift lasts, in minutes: past midnight when it ends at or
    before it starts, a whole day when the two are the same."""
    span = (end.hour * 60 + end.minute - start.hour * 60 - start.minute) % DAY
    return span or DAY


def shifts_from(value) -> tuple[Shift | None, ...]:
    """The stored shop hours (seven entries, Monday first, each ["HH:MM",
    "HH:MM"] or null) as times. Anything unreadable counts as a day off."""
    days: list[Shift | None] = []
    for i in range(7):
        try:
            start, end = value[i]
            days.append((dt.time.fromisoformat(start), dt.time.fromisoformat(end)))
        except IndexError, KeyError, TypeError, ValueError:
            days.append(None)
    return tuple(days)


def stored(shifts: Sequence[Shift | None]) -> list[list[str] | None]:
    """`shifts` the way BusinessProfile.shop_hours keeps them."""
    return [[t.strftime("%H:%M") for t in shift] if shift else None for shift in shifts]


#: No shop hours at all: work goes on around the clock.
ALL_DAY: tuple[Shift, ...] = ((dt.time(0), dt.time(0)),) * 7


@dataclass(frozen=True)
class Hours:
    shifts: tuple[Shift | None, ...]  # by Python weekday: Monday is 0
    zone: dt.tzinfo

    @classmethod
    def same_days(
        cls, days: Iterable[int], start: dt.time, end: dt.time, zone: dt.tzinfo = dt.UTC
    ) -> Hours:
        """The same hours on each of `days`."""
        days = set(days)
        return cls(tuple((start, end) if d in days else None for d in range(7)), zone)

    @classmethod
    def load(cls) -> Hours:
        from .models import BusinessProfile

        profile = BusinessProfile.load()
        try:
            zone = zoneinfo.ZoneInfo(profile.timezone)
        except zoneinfo.ZoneInfoNotFoundError, ValueError:
            zone = dt.UTC
        return cls(shifts_from(profile.shop_hours), zone)

    @property
    def usable(self) -> bool:
        return any(self.shifts)

    @property
    def _week(self) -> tuple[Shift | None, ...]:
        return self.shifts if self.usable else ALL_DAY

    def is_working_day(self, day: dt.date) -> bool:
        return self._week[day.weekday()] is not None

    def shift_on(self, day: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
        """When the shift starting on `day` begins and ends (wall clock)."""
        shift = self._week[day.weekday()]
        if shift is None:
            return None
        begins = dt.datetime.combine(day, shift[0])
        return begins, begins + dt.timedelta(minutes=shift_minutes(*shift))

    def minutes_on(self, day: dt.date) -> int:
        """Working minutes in the shift starting on `day`."""
        shift = self._week[day.weekday()]
        return shift_minutes(*shift) if shift else 0

    def available_minutes(self, first: dt.date, last: dt.date) -> int:
        """Working minutes in the shifts starting from `first` through `last`."""
        return sum(
            self.minutes_on(first + dt.timedelta(days=i)) for i in range((last - first).days + 1)
        )

    # Arithmetic is on the shop's wall clock (naive local times).

    def _shift_at(self, t: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
        """The part of the shift `t` falls in from `t` on, or else the next shift."""
        for offset in range(-1, 8):
            span = self.shift_on(t.date() + dt.timedelta(days=offset))
            if span and span[1] > t:
                return max(span[0], t), span[1]
        raise AssertionError("some day is a working day")

    def _add_work(self, t: dt.datetime, minutes: Decimal) -> dt.datetime:
        remaining = dt.timedelta(minutes=float(minutes))
        while True:
            t, ends = self._shift_at(t)
            if remaining <= ends - t:
                return t + remaining
            remaining -= ends - t
            t = ends

    def local(self, moment: dt.datetime) -> dt.datetime:
        return moment.astimezone(self.zone).replace(tzinfo=None)

    def aware(self, wall: dt.datetime) -> dt.datetime:
        return wall.replace(tzinfo=self.zone)

    def finish(self, start: dt.datetime, steps: Iterable[tuple[Decimal, Decimal]]) -> dt.datetime:
        """When `steps` — (work minutes, wait hours) each, in order — are done,
        starting at the aware datetime `start`."""
        t = self.local(start)
        for work, wait in steps:
            if work:
                t = self._add_work(t, work)
            if wait:
                t += dt.timedelta(hours=float(wait))
        return self.aware(t)

    def first_working_moment(self, day: dt.date) -> dt.datetime:
        """The start of the first shift starting on or after `day` (aware)."""
        for offset in range(7):
            span = self.shift_on(day + dt.timedelta(days=offset))
            if span:
                return self.aware(span[0])
        raise AssertionError("some day is a working day")

    def working_days_between(self, start: dt.datetime, end: dt.datetime) -> int:
        """Shifts any of the span from `start` to `end` falls in (so work started
        and finished in one shift is 1, and a cure that's over before the next
        shift begins doesn't count that shift)."""
        first, last = self.local(start), self.local(end)
        if last <= first:
            return 0
        count, day = 0, first.date() - dt.timedelta(days=1)
        while day <= last.date():
            span = self.shift_on(day)
            count += bool(span and span[0] < last and span[1] > first)
            day += dt.timedelta(days=1)
        return count


@dataclass(frozen=True)
class Estimate:
    work_minutes: Decimal
    wait_hours: Decimal
    start: dt.datetime
    finish: dt.datetime
    working_days: int


def estimate(steps: list[tuple[Decimal, Decimal]], start: dt.datetime | None = None) -> Estimate:
    """How long `steps` take, from `start` (default: the start of the next
    working day)."""
    from django.utils import timezone

    hours = Hours.load()
    if start is None:
        start = hours.first_working_moment(timezone.localdate() + dt.timedelta(days=1))
    finish = hours.finish(start, steps)
    return Estimate(
        work_minutes=sum((w for w, _ in steps), ZERO),
        wait_hours=sum((h for _, h in steps), ZERO),
        start=start,
        finish=finish,
        working_days=hours.working_days_between(start, finish),
    )
