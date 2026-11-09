from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from django.http import Http404
from ninja import ModelSchema, Router, Schema, Status

from trammel.core import access
from trammel.jobs.models import Job
from trammel.quotes.models import QuoteRevision
from trammel.shop.models import Material

from . import services
from .models import Operation, WorkOrder

router = Router(tags=["work orders"])


class OperationOut(ModelSchema):
    class Meta:
        model = Operation
        fields = ["id", "position", "name", "description", "machine", "status", "completed_at"]


class WorkOrderOut(ModelSchema):
    operations: list[OperationOut]

    class Meta:
        model = WorkOrder
        fields = [
            "id",
            "number",
            "job",
            "title",
            "instructions",
            "status",
            "waiting_on",
            "target_completion",
            "materials_confirmed",
            "created_at",
        ]

    @staticmethod
    def resolve_operations(obj):
        return list(obj.operations.all())


class WorkOrderCreateIn(Schema):
    job: UUID
    title: str = ""
    revision: UUID | None = None


class WorkOrderTransitionIn(Schema):
    target: str
    reason: str = ""
    waiting_on: str = ""


class OperationIn(Schema):
    state: str


class NoteIn(Schema):
    text: str


class MaterialUseIn(Schema):
    material: UUID | None = None
    description: str = ""
    quantity: Decimal
    unit: str = ""
    unit_cost: Decimal | None = None
    billable: bool = True


def _wo(request, perm, pk) -> WorkOrder:
    return access.get_or_404(request.actor, perm, WorkOrder.objects.all(), pk=pk)


@router.get("/", response=list[WorkOrderOut])
def list_work_orders(request, job: UUID | None = None, status: str | None = None):
    qs = WorkOrder.objects.all()
    if job:
        qs = qs.filter(job_id=job)
    if status:
        qs = qs.filter(status=status)
    return access.scoped(request.actor, "workorders.view", qs).order_by("-created_at")[:200]


@router.get("/{wo_id}", response=WorkOrderOut)
def get_work_order(request, wo_id: UUID):
    return _wo(request, "workorders.view", wo_id)


@router.post("/", response={201: WorkOrderOut})
def create(request, payload: WorkOrderCreateIn):
    access.require(request.actor, "workorders.create")
    job = access.get_or_404(request.actor, "jobs.view", Job.objects.all(), pk=payload.job)
    revision = (
        QuoteRevision.objects.filter(pk=payload.revision, quote__job=job, status="approved").first()
        if payload.revision
        else None
    )
    return Status(201, services.create(job, title=payload.title, revision=revision))


@router.post("/{wo_id}/transition", response=WorkOrderOut)
def transition(request, wo_id: UUID, payload: WorkOrderTransitionIn):
    wo = _wo(request, "workorders.view", wo_id)
    services.transition(
        wo,
        payload.target,
        actor=request.actor,
        reason=payload.reason,
        waiting_on=payload.waiting_on,
    )
    wo.refresh_from_db()
    return wo


@router.post("/{wo_id}/operations/{op_id}", response=WorkOrderOut)
def set_operation(request, wo_id: UUID, op_id: UUID, payload: OperationIn):
    wo = _wo(request, "workorders.edit", wo_id)
    op = wo.operations.filter(pk=op_id).first()
    if op is None:
        raise Http404
    services.set_operation(op, payload.state, user=request.actor.user)
    wo.refresh_from_db()
    return wo


@router.post("/{wo_id}/notes", response={201: dict})
def add_note(request, wo_id: UUID, payload: NoteIn):
    note = services.add_note(
        _wo(request, "workorders.edit", wo_id), text=payload.text, user=request.actor.user
    )
    return Status(201, {"id": str(note.pk)})


@router.post("/{wo_id}/materials", response={201: dict})
def record_material(request, wo_id: UUID, payload: MaterialUseIn):
    wo = _wo(request, "workorders.edit", wo_id)
    material = Material.objects.filter(pk=payload.material).first() if payload.material else None
    usage = services.record_material(
        wo,
        material=material,
        quantity=payload.quantity,
        unit=payload.unit,
        description=payload.description,
        unit_cost=payload.unit_cost,
        billable=payload.billable,
        user=request.actor.user,
    )
    return Status(201, {"id": str(usage.pk), "total_cost": str(usage.total_cost)})
