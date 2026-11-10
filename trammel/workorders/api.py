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
    #: "1.2.3": its place in the list, sub-operations under their parent.
    number: str
    parent: UUID | None

    class Meta:
        model = Operation
        fields = [
            "id",
            "position",
            "depth",
            "name",
            "description",
            "machine",
            "status",
            "completed_at",
        ]

    @staticmethod
    def resolve_parent(obj):
        return obj.parent_id


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
        return services.operation_tree(obj)


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


class OperationCreateIn(Schema):
    name: str
    description: str = ""
    machine: UUID | None = None
    #: An operation on the same work order to add this under (up to four levels).
    parent: UUID | None = None


class NoteIn(Schema):
    text: str


class MaterialUseIn(Schema):
    material: UUID | None = None
    description: str = ""
    quantity: Decimal
    unit: str = ""
    unit_cost: Decimal | None = None
    billable: bool = True
    #: A stock lot to draw from, for a material tracked in stock (else oldest first).
    lot: UUID | None = None
    #: A piece of what was used, kept as a remnant: length, optional width, unit.
    leftover_length: Decimal | None = None
    leftover_width: Decimal | None = None
    leftover_unit: str = "in"


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


@router.post("/{wo_id}/operations", response={201: WorkOrderOut})
def add_operation(request, wo_id: UUID, payload: OperationCreateIn):
    from django.core.exceptions import ValidationError

    from trammel.shop.models import Machine

    wo = _wo(request, "workorders.edit", wo_id)
    parent = None
    if payload.parent is not None:
        parent = wo.operations.filter(pk=payload.parent).first()
        if parent is None:
            raise ValidationError({"parent": ["Not an operation on this work order."]})
    machine = None
    if payload.machine is not None:
        machine = Machine.objects.filter(pk=payload.machine).first()
        if machine is None:
            raise ValidationError({"machine": ["Unknown machine."]})
    if not payload.name.strip():
        raise ValidationError({"name": ["Name the operation."]})
    services.add_operation(
        wo,
        name=payload.name.strip(),
        description=payload.description,
        machine=machine,
        parent=parent,
    )
    wo.refresh_from_db()
    return Status(201, wo)


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
    usages = services.record_material(
        wo,
        material=material,
        quantity=payload.quantity,
        unit=payload.unit,
        description=payload.description,
        unit_cost=payload.unit_cost,
        billable=payload.billable,
        lot=payload.lot,
        leftover=(
            (payload.leftover_length, payload.leftover_width, payload.leftover_unit)
            if payload.leftover_length
            else None
        ),
        user=request.actor.user,
    )
    total = sum((u.total_cost for u in usages), Decimal("0"))
    return Status(
        201,
        {"id": str(usages[0].pk), "ids": [str(u.pk) for u in usages], "total_cost": str(total)},
    )
