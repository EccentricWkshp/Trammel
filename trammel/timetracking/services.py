"""Timers and time entries (FR-TIME-1..4)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from trammel.core import audit
from trammel.core.models import ShopRates
from trammel.core.money import round_money

from .models import TimeEntry


def _rates(kind: str, user=None, machine=None) -> tuple[Decimal, Decimal]:
    """(cost rate, bill rate) per hour (FR-TIME-4)."""
    shop = ShopRates.load()
    if kind == TimeEntry.Kind.MACHINE:
        return machine.cost_rate, machine.hourly_rate
    cost = user.labor_cost_rate if user.labor_cost_rate is not None else shop.labor_hourly_rate
    return cost, shop.labor_hourly_rate


def _finish(entry: TimeEntry) -> None:
    entry.cost = round_money(entry.minutes / 60 * entry.cost_rate)


def _check_work_order(wo) -> None:
    if wo.status not in ("open", "in_progress", "waiting"):
        raise ValidationError(
            f"{wo.number} is {wo.get_status_display().lower()}; time cannot "
            "be recorded on it. Reopen it first."
        )


@transaction.atomic
def start(
    work_order,
    *,
    kind: str = TimeEntry.Kind.LABOR,
    user=None,
    machine=None,
    operation=None,
    note: str = "",
) -> TimeEntry:
    from trammel.workorders.services import ensure_started

    _check_work_order(work_order)
    if kind == TimeEntry.Kind.LABOR and user is None:
        raise ValidationError("A labor timer needs a person.")
    if kind == TimeEntry.Kind.MACHINE and machine is None:
        raise ValidationError("A machine timer needs a machine.")
    cost, bill = _rates(kind, user, machine)
    try:
        with transaction.atomic():
            entry = TimeEntry.objects.create(
                work_order=work_order,
                operation=operation,
                kind=kind,
                user=user if kind == TimeEntry.Kind.LABOR else None,
                machine=machine if kind == TimeEntry.Kind.MACHINE else None,
                started_at=timezone.now(),
                cost_rate=cost,
                bill_rate=bill,
                note=note,
                created_by=audit.current_actor().user,
            )
    except IntegrityError:
        who = "You already have" if kind == TimeEntry.Kind.LABOR else f"{machine} already has"
        raise ValidationError(f"{who} a running timer. Stop it first.") from None
    ensure_started(work_order)
    audit.record(
        "time.start",
        work_order,
        summary=f"Started {kind} timer on {work_order.number}"
        + (f" for {machine}" if machine else ""),
    )
    return entry


@transaction.atomic
def stop(entry: TimeEntry, *, at=None) -> TimeEntry:
    if not entry.is_running:
        return entry
    entry.ended_at = at or timezone.now()
    entry.minutes = Decimal(
        max(0, (entry.ended_at - entry.started_at).total_seconds()) / 60
    ).quantize(Decimal("0.01"))
    _finish(entry)
    entry.save(update_fields=["ended_at", "minutes", "cost", "updated_at"])
    audit.record(
        "time.stop",
        entry.work_order,
        summary=f"Stopped timer on {entry.work_order.number} ({entry.minutes} min)",
    )
    return entry


def running_for(user):
    return TimeEntry.objects.filter(user=user, kind=TimeEntry.Kind.LABOR, ended_at__isnull=True)


@transaction.atomic
def manual(
    work_order,
    *,
    kind: str,
    minutes: Decimal,
    started_at,
    user=None,
    machine=None,
    operation=None,
    billable: bool = True,
    note: str = "",
) -> TimeEntry:
    """A manual entry (FR-TIME-3)."""
    _check_work_order(work_order)
    if minutes <= 0:
        raise ValidationError("Enter the time worked.")
    cost, bill = _rates(kind, user, machine)
    entry = TimeEntry(
        work_order=work_order,
        operation=operation,
        kind=kind,
        user=user if kind == TimeEntry.Kind.LABOR else None,
        machine=machine if kind == TimeEntry.Kind.MACHINE else None,
        started_at=started_at,
        ended_at=started_at + dt.timedelta(minutes=float(minutes)),
        minutes=minutes,
        cost_rate=cost,
        bill_rate=bill,
        billable=billable,
        note=note,
        source=TimeEntry.Source.MANUAL,
        created_by=audit.current_actor().user,
    )
    _finish(entry)
    entry.full_clean()
    entry.save()
    audit.record_create(entry, summary=f"Manual time entry on {work_order.number}: {minutes} min")
    return entry


@transaction.atomic
def correct(entry: TimeEntry, *, before: dict, reason: str = "") -> TimeEntry:
    """Correct an entry; every change is audit-logged with its reason (FR-TIME-3)."""
    if entry.invoice_line_id:
        raise ValidationError("This time has been invoiced; correct it with a credit note.")
    if entry.ended_at and entry.ended_at < entry.started_at:
        raise ValidationError("The end is before the start.")
    if entry.ended_at and entry.source == TimeEntry.Source.TIMER:
        entry.minutes = Decimal((entry.ended_at - entry.started_at).total_seconds() / 60).quantize(
            Decimal("0.01")
        )
    _finish(entry)
    entry.save()
    audit.record_update(
        entry, before, summary=f"Corrected time entry on {entry.work_order.number}", reason=reason
    )
    return entry
