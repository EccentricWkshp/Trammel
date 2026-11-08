from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

from django.http import Http404
from ninja import ModelSchema, Router, Schema, Status

from trammel.core import access
from trammel.shop.models import Machine
from trammel.workorders.models import WorkOrder

from . import services
from .models import TimeEntry

router = Router(tags=["time"])


class EntryOut(ModelSchema):
    class Meta:
        model = TimeEntry
        fields = [
            "id",
            "work_order",
            "operation",
            "kind",
            "user",
            "machine",
            "started_at",
            "ended_at",
            "minutes",
            "billable",
            "note",
            "source",
        ]


class StartIn(Schema):
    work_order: UUID
    kind: str = "labor"
    machine: UUID | None = None


class ManualIn(Schema):
    work_order: UUID
    kind: str = "labor"
    machine: UUID | None = None
    started_at: dt.datetime
    minutes: Decimal
    billable: bool = True
    note: str = ""


def _wo(request, pk):
    return access.get_or_404(request.actor, "time.track", WorkOrder.objects.all(), pk=pk)


def _machine(pk):
    return Machine.objects.filter(pk=pk).first() if pk else None


@router.get("/", response=list[EntryOut])
def list_entries(request, work_order: UUID | None = None, running: bool | None = None):
    qs = TimeEntry.objects.all()
    if work_order:
        qs = qs.filter(work_order_id=work_order)
    if running is not None:
        qs = qs.filter(ended_at__isnull=running)
    if not access.has_perm(request.actor, "time.manage"):
        qs = qs.filter(user=request.actor.user)
    return access.scoped(request.actor, "time.view", qs)[:500]


@router.post("/start", response={201: EntryOut})
def start(request, payload: StartIn):
    access.require(request.actor, "time.track")
    return Status(
        201,
        services.start(
            _wo(request, payload.work_order),
            kind=payload.kind,
            user=request.actor.user,
            machine=_machine(payload.machine),
        ),
    )


@router.post("/{entry_id}/stop", response=EntryOut)
def stop(request, entry_id: UUID):
    entry = access.get_or_404(request.actor, "time.track", TimeEntry.objects.all(), pk=entry_id)
    if (
        entry.kind == "labor"
        and entry.user_id != getattr(request.actor.user, "pk", None)
        and not access.has_perm(request.actor, "time.manage")
    ):
        raise Http404
    return services.stop(entry)


@router.post("/manual", response={201: EntryOut})
def manual(request, payload: ManualIn):
    access.require(request.actor, "time.track")
    return Status(
        201,
        services.manual(
            _wo(request, payload.work_order),
            kind=payload.kind,
            minutes=payload.minutes,
            started_at=payload.started_at,
            user=request.actor.user,
            machine=_machine(payload.machine),
            billable=payload.billable,
            note=payload.note,
        ),
    )
