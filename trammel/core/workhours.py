"""The shop's working hours, for estimating when work will be done.

Work (hands-on and machine time) only happens in working hours: on the shop's
working days, between the start and end of its day. Waiting (glue clamping,
finish curing, paint drying) goes on around the clock, nights and days off
included. Steps are taken one at a time, in order: each step's work, then its
wait, before the next one starts.
"""

from __future__ import annotations

import datetime as dt
import zoneinfo
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")


@dataclass(frozen=True)
class Hours:
    days: frozenset[int]  # Python weekdays: Monday is 0
    start: dt.time
    end: dt.time
    zone: dt.tzinfo

    @classmethod
    def load(cls) -> Hours:
        from .models import BusinessProfile

        profile = BusinessProfile.load()
        try:
            zone = zoneinfo.ZoneInfo(profile.timezone)
        except zoneinfo.ZoneInfoNotFoundError, ValueError:
            zone = dt.UTC
        return cls(frozenset(profile.work_days), profile.day_starts, profile.day_ends, zone)

    @property
    def usable(self) -> bool:
        return bool(self.days) and self.start < self.end

    @property
    def day_minutes(self) -> int:
        """Working minutes in a working day."""
        if not self.usable:
            return 24 * 60
        today = dt.date.today()
        span = dt.datetime.combine(today, self.end) - dt.datetime.combine(today, self.start)
        return int(span.total_seconds() // 60)

    def is_working_day(self, day: dt.date) -> bool:
        return not self.usable or day.weekday() in self.days

    # Arithmetic is on the shop's wall clock (naive local times).

    def _next_working(self, t: dt.datetime) -> dt.datetime:
        """`t` if it's in working hours, otherwise when they next begin."""
        if not self.usable:
            return t
        day = t.date()
        if day.weekday() in self.days:
            if t.time() < self.start:
                return dt.datetime.combine(day, self.start)
            if t.time() < self.end:
                return t
        for _ in range(7):
            day += dt.timedelta(days=1)
            if day.weekday() in self.days:
                return dt.datetime.combine(day, self.start)
        return t  # unreachable: some day is a working day

    def _add_work(self, t: dt.datetime, minutes: Decimal) -> dt.datetime:
        remaining = dt.timedelta(minutes=float(minutes))
        if not self.usable:
            return t + remaining
        t = self._next_working(t)
        while remaining > dt.timedelta(0):
            left_today = dt.datetime.combine(t.date(), self.end) - t
            if remaining <= left_today:
                return t + remaining
            remaining -= left_today
            t = self._next_working(dt.datetime.combine(t.date(), self.end))
        return t

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
        """The start of the first working day on or after `day` (aware)."""
        wall = self._next_working(dt.datetime.combine(day, dt.time.min))
        return self.aware(wall)

    def working_days_between(self, start: dt.datetime, end: dt.datetime) -> int:
        """Working days from `start` through `end`, counting each one any of the
        span falls in (so work started and finished on one day is 1)."""
        first, last = self.local(start), self.local(end)
        if last <= first:
            return 0
        if self.usable and last.time() <= self.start:
            # Ready before the day's work starts: the day before is the last.
            last_day = last.date() - dt.timedelta(days=1)
        else:
            last_day = last.date()
        count, day = 0, first.date()
        while day <= last_day:
            count += self.is_working_day(day)
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
