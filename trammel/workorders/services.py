"""Work order and change order writes (FR-WO). Web, API and the shop-floor app call these."""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from trammel.core import audit, numbering
from trammel.core.access import SYSTEM
from trammel.core.money import round_money
from trammel.core.services import save_audited
from trammel.shop.models import Machine, Material

from .models import (
    ChangeOrder,
    ChangeOrderLine,
    COStatus,
    MaterialUsage,
    Operation,
    PlannedMaterial,
    Status,
    WorkOrder,
    WorkOrderNote,
)

ZERO = Decimal("0")


def can_create_for(job) -> list[str]:
    from trammel.agreements import services as agreements

    problems = []
    if job.status not in ("approved", "in_progress"):
        problems.append("Work orders are created once the job is approved.")
    if not agreements.satisfied(job):
        problems.append("The customer agreement must be accepted (or waived) first.")
    return problems


@transaction.atomic
def create(job, *, title: str = "", revision=None) -> WorkOrder:
    """A work order, carrying scope, operations and materials from a quote (FR-WO-1)."""
    problems = can_create_for(job)
    if problems:
        raise ValidationError(problems)
    wo = WorkOrder(
        job=job,
        title=title or job.title,
        number=numbering.next_number("work_order"),
        target_completion=job.target_completion,
        source_revision=revision,
        instructions=job.description,
    )
    save_audited(wo, summary=f"Created work order for {job.number}")
    if revision is not None:
        _carry_over(wo, revision)
    return wo


def _carry_over(wo: WorkOrder, revision) -> None:
    position = 0
    for line in revision.lines.select_related("machine", "material"):
        position += 10
        machine = line.machine
        if machine is None and line.inputs.get("machine"):
            machine = Machine.objects.filter(pk=line.inputs["machine"]).first()
        Operation.objects.create(
            work_order=wo, position=position, name=line.description[:200], machine=machine
        )
        material = line.material
        if material is None and line.inputs.get("material"):
            material = Material.objects.filter(pk=line.inputs["material"]).first()
        if material is not None:
            row = next((r for r in line.breakdown if r.get("category") == "materials"), None)
            PlannedMaterial.objects.create(
                work_order=wo,
                material=material,
                description=row["detail"] if row else str(material),
                quantity=line.quantity if line.kind == "material" else None,
                unit=line.unit if line.kind == "material" else "",
            )


@transaction.atomic
def transition(
    wo: WorkOrder, target: str, *, actor, reason: str = "", waiting_on: str = ""
) -> None:
    wo.refresh_from_db(fields=["status"])
    t = WorkOrder.lifecycle.find(wo, target)
    if t is None:
        from trammel.core.lifecycle import TransitionError

        raise TransitionError([f"Cannot move from “{wo.get_status_display()}” to “{target}”."])
    if target == Status.WAITING and waiting_on not in WorkOrder.WaitingOn.values:
        raise ValidationError("Say what the work order is waiting on.")
    WorkOrder.lifecycle.apply(wo, target, actor=actor, reason=reason, kind=t.kind)
    WorkOrder.objects.filter(pk=wo.pk).update(
        waiting_on=waiting_on if target == Status.WAITING else ""
    )


def ensure_started(wo: WorkOrder) -> None:
    """Recording work on an open work order starts it."""
    if wo.status == Status.OPEN:
        WorkOrder.lifecycle.apply(wo, Status.IN_PROGRESS, actor=SYSTEM)
        wo.refresh_from_db()


def _require_editable(wo: WorkOrder) -> None:
    if not wo.is_editable:
        raise ValidationError(f"{wo.number} is {wo.get_status_display().lower()}. Reopen it first.")


@transaction.atomic
def set_operation(op: Operation, state: str, *, user) -> Operation:
    """Mark an operation done, skipped, or back to do (FR-WO-2, FR-WO-8)."""
    _require_editable(op.work_order)
    if state not in Operation.State.values:
        raise ValidationError("Unknown operation state.")
    before = op.status
    if state != Operation.State.PENDING:
        ensure_started(op.work_order)
    op.status = state
    op.completed_at = timezone.now() if state != Operation.State.PENDING else None
    op.completed_by = user if state != Operation.State.PENDING else None
    op.save()
    audit.record(
        "operation.reopen" if state == Operation.State.PENDING else f"operation.{state}",
        op.work_order,
        summary=f"{op.work_order.number}: “{op.name}” {before} → {state}",
    )
    return op


@transaction.atomic
def add_operation(wo: WorkOrder, *, name: str, description: str = "", machine=None) -> Operation:
    _require_editable(wo)
    last = wo.operations.aggregate(m=models.Max("position"))["m"] or 0
    op = Operation.objects.create(
        work_order=wo, position=last + 10, name=name, description=description, machine=machine
    )
    audit.record("operation.add", wo, summary=f"Added operation “{name}” to {wo.number}")
    return op


@transaction.atomic
def update_operation(op: Operation, *, name: str, description: str = "", machine=None) -> Operation:
    _require_editable(op.work_order)
    name = name.strip()
    if not name:
        raise ValidationError("Name the operation.")
    before = audit.snapshot(op)
    op.name, op.description, op.machine = name, description.strip(), machine
    op.save()
    audit.record(
        "operation.edit",
        op.work_order,
        before=before,
        after=audit.snapshot(op),
        summary=f"{op.work_order.number}: edited operation “{op.name}”",
    )
    return op


@transaction.atomic
def delete_operation(op: Operation) -> int:
    """Remove an operation. Time logged against it stays on the work order."""
    wo = op.work_order
    _require_editable(wo)
    detached = op.time_entries.count()
    name = op.name
    op.delete()
    audit.record("operation.delete", wo, summary=f"{wo.number}: deleted operation “{name}”")
    return detached


@transaction.atomic
def move_operation(op: Operation, direction: int) -> None:
    """Swap an operation with its neighbor above (-1) or below (+1)."""
    wo = op.work_order
    _require_editable(wo)
    ops = list(wo.operations.select_for_update().order_by("position", "id"))
    i = next(n for n, o in enumerate(ops) if o.pk == op.pk)
    j = i + direction
    if not 0 <= j < len(ops):
        return
    ops[i], ops[j] = ops[j], ops[i]
    for n, o in enumerate(ops, start=1):
        if o.position != n * 10:
            o.position = n * 10
            o.save(update_fields=["position"])
    audit.record("operation.move", wo, summary=f"{wo.number}: moved operation “{op.name}”")


@transaction.atomic
def add_note(wo: WorkOrder, *, text: str = "", upload=None, user=None) -> WorkOrderNote:
    """Append a note or photo (FR-WO-6, FR-DOC-7)."""
    if not text.strip() and upload is None:
        raise ValidationError("Write a note or attach a photo.")
    document = None
    if upload is not None:
        from trammel.files.services import upload as store

        document = store(upload, name=upload.name, attach_to=wo, user=user).document
    note = WorkOrderNote.objects.create(
        work_order=wo, text=text.strip(), document=document, author=user
    )
    audit.record("work_order.note", wo, summary=f"Note on {wo.number}")
    return note


@transaction.atomic
def record_material(
    wo: WorkOrder,
    *,
    material: Material | None,
    quantity: Decimal,
    unit: str,
    description: str = "",
    unit_cost: Decimal | None = None,
    note: str = "",
    billable: bool = True,
    user=None,
) -> MaterialUsage:
    """Material consumed on a work order (FR-STK-8 in Phase 1 form). Negative corrects."""
    _require_editable(wo)
    if not quantity:
        raise ValidationError("Enter a quantity.")
    if material is not None:
        unit = unit or material.unit
        unit_cost = material.cost_per(unit) if unit_cost is None else unit_cost
        description = description or str(material)
    if unit_cost is None or not description:
        raise ValidationError("Pick a material, or describe it and give its cost.")
    ensure_started(wo)
    usage = MaterialUsage.objects.create(
        work_order=wo,
        material=material,
        description=description,
        quantity=quantity,
        unit=unit,
        unit_cost=unit_cost,
        total_cost=round_money(quantity * unit_cost),
        note=note,
        billable=billable,
        recorded_by=user,
    )
    audit.record(
        "work_order.material", wo, summary=f"{wo.number}: {quantity} {unit} of {description}"
    )
    return usage


@transaction.atomic
def confirm_materials(wo: WorkOrder, confirmed: bool = True) -> None:
    WorkOrder.objects.filter(pk=wo.pk).update(materials_confirmed=confirmed)
    wo.materials_confirmed = confirmed
    audit.record(
        "work_order.materials_confirmed",
        wo,
        summary=f"{wo.number}: material use {'confirmed' if confirmed else 'unconfirmed'}",
    )


# ---------------------------------------------------------------------------
# Change orders
# ---------------------------------------------------------------------------


@transaction.atomic
def create_change_order(job, *, title: str, description: str = "") -> ChangeOrder:
    if job.status not in ("approved", "in_progress", "completed"):
        raise ValidationError("Change orders amend an approved job.")
    co = ChangeOrder(
        job=job, title=title, description=description, number=numbering.next_number("change_order")
    )
    return save_audited(co)


def _co_total(co: ChangeOrder) -> None:
    co.total = sum((line.line_total for line in co.lines.all()), ZERO)
    ChangeOrder.objects.filter(pk=co.pk).update(total=co.total)


@transaction.atomic
def save_co_line(line: ChangeOrderLine) -> ChangeOrderLine:
    if line.change_order.status != COStatus.DRAFT:
        raise ValidationError("Lines can only change on a draft change order.")
    line.line_total = round_money(Decimal(line.quantity) * Decimal(line.unit_price))
    line.save()
    _co_total(line.change_order)
    return line


@transaction.atomic
def delete_co_line(line: ChangeOrderLine) -> None:
    co = line.change_order
    if co.status != COStatus.DRAFT:
        raise ValidationError("Lines can only change on a draft change order.")
    line.delete()
    _co_total(co)


@transaction.atomic
def approve_change_order(
    co: ChangeOrder, *, actor, approved_by_name: str, how: str = "", user=None
) -> ChangeOrder:
    """Approve like a quote (FR-WO-5); amended terms are accepted with it (FR-AGR-6)."""
    if not approved_by_name.strip():
        raise ValidationError("Who approved the change order?")
    ChangeOrder.lifecycle.apply(co, COStatus.APPROVED, actor=actor)
    agreement = None
    if co.amended_terms.strip():
        from trammel.agreements import services as agreements
        from trammel.agreements.models import AgreementTemplate

        template = AgreementTemplate.default()
        agreement = agreements.issue(co.job, template=template, primary=False)
        agreement.text = f"Amended terms — {co.number}\n\n{co.amended_terms.strip()}"
        agreement.save(update_fields=["text"])
        agreements.accept(
            agreement,
            method="manual",
            accepted_by_name=approved_by_name,
            how=how,
            notes=f"Accepted with change order {co.number}",
            user=user,
        )
    ChangeOrder.objects.filter(pk=co.pk).update(
        approved_by_name=approved_by_name.strip(),
        approved_at=timezone.now(),
        approval_how=how,
        agreement=agreement,
    )
    # Flow into the work: an operation on the job's first active work order.
    wo = co.job.work_orders.filter(status__in=["open", "in_progress", "waiting"]).first()
    if wo is not None:
        add_operation(
            wo, name=f"Change order {co.number}: {co.title}"[:200], description=co.description
        )
    audit.record(
        "change_order.approve", co, summary=f"{approved_by_name} approved {co.number} ({co.total})"
    )
    co.refresh_from_db()
    return co
