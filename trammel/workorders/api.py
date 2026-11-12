from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from django.http import Http404
from ninja import ModelSchema, Router, Schema, Status

from trammel.core import access
from trammel.jobs.models import Job
from trammel.quotes.models import QuoteRevision
from trammel.shop.models import Material

from . import services
from .models import IncludedItem, Operation, WorkOrder

router = Router(tags=["work orders"])


class OperationOut(ModelSchema):
    #: "1.2.3": its place in the list, sub-operations under their parent.
    number: str
    parent: UUID | None
    #: Minutes logged against it and everything under it, by kind.
    logged_labor_minutes: Decimal
    logged_machine_minutes: Decimal
    #: The machine the shop schedule has it on: its own, or another in the
    #: machine's group that finishes it sooner. Null when it isn't scheduled.
    scheduled_machine: UUID | None
    #: Pinned to a day before the steps ahead of it can be done.
    pinned_too_early: bool

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
            "planned_labor_minutes",
            "planned_machine_minutes",
            "planned_wait_hours",
            "pinned_date",
            "pinned_from",
            "pinned_overtime",
            "machine_pinned",
        ]

    @staticmethod
    def resolve_parent(obj):
        return obj.parent_id

    @staticmethod
    def resolve_scheduled_machine(obj):
        plan = getattr(obj, "_plan", None)
        return plan.machine_id if plan and plan.bookings else None

    @staticmethod
    def resolve_pinned_too_early(obj):
        plan = getattr(obj, "_plan", None)
        return bool(plan and plan.too_early)

    @staticmethod
    def resolve_logged_labor_minutes(obj):
        return getattr(obj, "time", {}).get("logged_labor", 0)

    @staticmethod
    def resolve_logged_machine_minutes(obj):
        return getattr(obj, "time", {}).get("logged_machine", 0)


class IncludedItemOut(ModelSchema):
    """A resold product to set aside or pack with the work, checked off rather
    than worked on."""

    #: The change order it came from, if not the work order's own quote.
    source_revision: UUID | None

    class Meta:
        model = IncludedItem
        fields = [
            "id",
            "product",
            "description",
            "quantity",
            "unit",
            "position",
            "status",
            "completed_at",
        ]

    @staticmethod
    def resolve_source_revision(obj):
        return obj.source_revision_id


class WorkOrderOut(ModelSchema):
    operations: list[OperationOut]
    #: Items to include: resold products from the quote.
    items: list[IncludedItemOut]
    #: When the work still to do should be finished: in the shop schedule,
    #: behind the work ahead of it in the queue, or for a work order the
    #: schedule doesn't plan (waiting, say) on its own. Null when nothing left
    #: has planned time.
    estimated_finish: datetime | None
    #: When the shop schedule has it starting and finishing, and its place in
    #: the queue; null when it isn't planned.
    scheduled_start: datetime | None
    scheduled_finish: datetime | None
    queue_position: int | None
    #: Moved up or down the queue, away from where its priority and dates put it.
    queue_moved: bool

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
        obj._tree = services.operation_tree(obj)
        planned = WorkOrderOut._planned(obj)
        plans = {p.op.pk: p for p in planned.ops} if planned else {}
        for op in obj._tree:
            op._plan = plans.get(op.pk)
        return obj._tree

    @staticmethod
    def resolve_items(obj):
        return list(obj.items.all())

    @staticmethod
    def _planned(obj):
        if not hasattr(obj, "_planned"):
            from . import scheduling

            obj._planned = scheduling.build().for_work_order(obj)
        return obj._planned

    @staticmethod
    def resolve_estimated_finish(obj):
        planned = WorkOrderOut._planned(obj)
        if planned is not None:
            return planned.finish
        return services.estimate_finish(obj, getattr(obj, "_tree", None))

    @staticmethod
    def resolve_scheduled_start(obj):
        planned = WorkOrderOut._planned(obj)
        return planned.start if planned else None

    @staticmethod
    def resolve_scheduled_finish(obj):
        planned = WorkOrderOut._planned(obj)
        return planned.finish if planned else None

    @staticmethod
    def resolve_queue_position(obj):
        planned = WorkOrderOut._planned(obj)
        return planned.position if planned else None

    @staticmethod
    def resolve_queue_moved(obj):
        planned = WorkOrderOut._planned(obj)
        return bool(planned and planned.moved)


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


class OperationPinsIn(Schema):
    #: Plan it from this day's opening, ahead of the queue; null for no pin.
    pinned_date: date | None = None
    #: When that day to start it; null for the day's opening.
    pinned_from: time | None = None
    #: Work outside shop hours that day (early, late, a day off) to finish it.
    pinned_overtime: bool = False
    #: Keep it on its own machine rather than another in the machine's group.
    machine_pinned: bool = False


class QueueMoveIn(Schema):
    #: "up" or "down" one place, or "back" to where its priority and dates put it.
    direction: str


class OperationCreateIn(Schema):
    name: str
    description: str = ""
    machine: UUID | None = None
    #: An operation on the same work order to add this under (up to four levels).
    parent: UUID | None = None


class NoteIn(Schema):
    text: str
    #: Show it to the customer in the portal as an update on their job.
    shared_with_customer: bool = False


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
    found = list(access.scoped(request.actor, "workorders.view", qs).order_by("-created_at")[:200])
    # One shop plan for the lot, rather than one each.
    from . import scheduling

    plan = scheduling.build()
    for wo in found:
        wo._planned = plan.for_work_order(wo)
    return found


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


@router.post("/{wo_id}/operations/{op_id}/pins", response=WorkOrderOut)
def pin_operation(request, wo_id: UUID, op_id: UUID, payload: OperationPinsIn):
    """Pins an operation on the shop schedule: to a day, to its machine, or both."""
    wo = _wo(request, "workorders.edit", wo_id)
    op = wo.operations.filter(pk=op_id).first()
    if op is None:
        raise Http404
    services.pin_operation(
        op,
        pinned_date=payload.pinned_date,
        machine_pinned=payload.machine_pinned,
        pinned_from=payload.pinned_from,
        pinned_overtime=payload.pinned_overtime,
    )
    wo.refresh_from_db()
    return wo


@router.post("/{wo_id}/queue", response=WorkOrderOut)
def move_in_queue(request, wo_id: UUID, payload: QueueMoveIn):
    """Moves an open or in-progress work order in the schedule's queue."""
    wo = _wo(request, "workorders.edit", wo_id)
    services.move_in_queue(wo, payload.direction)
    wo.refresh_from_db()
    return wo


@router.post("/{wo_id}/items/{item_id}", response=WorkOrderOut)
def set_item(request, wo_id: UUID, item_id: UUID, payload: OperationIn):
    """Check an item to include off: `done` (included), `skipped` (not needed),
    or `pending`."""
    wo = _wo(request, "workorders.edit", wo_id)
    item = wo.items.filter(pk=item_id).first()
    if item is None:
        raise Http404
    services.set_item(item, payload.state, user=request.actor.user)
    wo.refresh_from_db()
    return wo


@router.post("/{wo_id}/notes", response={201: dict})
def add_note(request, wo_id: UUID, payload: NoteIn):
    note = services.add_note(
        _wo(request, "workorders.edit", wo_id),
        text=payload.text,
        user=request.actor.user,
        shared=payload.shared_with_customer,
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
