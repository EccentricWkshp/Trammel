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

from . import hooks
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
        if line.kind == "material" and line.material is not None:
            PlannedMaterial.objects.create(
                work_order=wo,
                material=line.material,
                description=str(line.material),
                quantity=line.quantity,
                unit=line.unit,
            )
            continue
        # Estimator lines say which material and how much in their breakdown.
        rows = [r for r in line.breakdown or [] if r.get("category") == "materials"]
        planned = False
        for row in rows:
            material = Material.objects.filter(pk=row.get("material")).first()
            if material is None:
                continue
            PlannedMaterial.objects.create(
                work_order=wo,
                material=material,
                description=row.get("detail") or str(material),
                quantity=Decimal(row["quantity"]) if row.get("quantity") else None,
                unit=row.get("unit", ""),
            )
            planned = True
        # Lines estimated before rows carried the material (0.2.x and earlier).
        material = line.material
        if material is None and line.inputs.get("material"):
            material = Material.objects.filter(pk=line.inputs["material"]).first()
        if material is not None and not planned:
            row = rows[0] if rows else None
            PlannedMaterial.objects.create(
                work_order=wo,
                material=material,
                description=row["detail"] if row else str(material),
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


# ---------------------------------------------------------------------------
# Operations and sub-operations (FR-WO-2)
# ---------------------------------------------------------------------------

P = Operation.State


def operation_tree(wo: WorkOrder) -> list[Operation]:
    """Every operation in reading order, each with `number` ("1.2.3"),
    `has_children`, and `is_first` / `is_last` among its siblings."""
    ops = list(wo.operations.select_related("machine", "completed_by"))
    children: dict = {}
    for op in ops:
        children.setdefault(op.parent_id, []).append(op)
    out: list[Operation] = []

    def walk(parent_id, prefix: str) -> None:
        siblings = children.get(parent_id, [])
        for n, op in enumerate(siblings, start=1):
            op.number = f"{prefix}{n}"
            op.has_children = op.pk in children
            op.is_first, op.is_last = n == 1, n == len(siblings)
            out.append(op)
            walk(op.pk, f"{op.number}.")

    walk(None, "")
    return out


def _descendants(op: Operation) -> list[Operation]:
    found, frontier = [], [op.pk]
    while frontier:
        level = list(Operation.objects.filter(parent_id__in=frontier))
        found += level
        frontier = [o.pk for o in level]
    return found


def _height(op: Operation, below: list[Operation] | None = None) -> int:
    """How many levels sit below `op` (0 when it has no sub-operations)."""
    below = _descendants(op) if below is None else below
    return max((o.depth for o in below), default=op.depth) - op.depth


def _too_deep() -> ValidationError:
    return ValidationError(f"Sub-operations go at most {Operation.MAX_DEPTH + 1} levels deep.")


def _roll_up(op: Operation | None, user) -> None:
    """A parent follows its sub-operations: done once none is left to do (skipped
    if every one was skipped), and back to do when one is reopened or added."""
    while op is not None:
        states = set(op.children.values_list("status", flat=True))
        if not states:
            return
        if P.PENDING in states:
            state = P.PENDING
        elif states == {P.SKIPPED}:
            state = P.SKIPPED
        else:
            state = P.DONE
        if state == op.status:
            return
        op.status = state
        op.completed_at = timezone.now() if state != P.PENDING else None
        op.completed_by = user if state != P.PENDING else None
        op.save(update_fields=["status", "completed_at", "completed_by"])
        op = op.parent


@transaction.atomic
def set_operation(op: Operation, state: str, *, user) -> Operation:
    """Mark an operation done, skipped, or back to do (FR-WO-2, FR-WO-8).

    An operation with sub-operations is done when they are, so it cannot be
    marked done while one is still to do. Skipping it skips those still to do.
    """
    _require_editable(op.work_order)
    if state not in P.values:
        raise ValidationError("Unknown operation state.")
    pending_below = [o for o in _descendants(op) if o.status == P.PENDING]
    if state == P.DONE and pending_below:
        raise ValidationError(
            f"Finish or skip the sub-operations of “{op.name}” first "
            f"({len(pending_below)} still to do)."
        )
    before = op.status
    if state != P.PENDING:
        ensure_started(op.work_order)
    now = timezone.now()
    if state == P.SKIPPED:
        for child in pending_below:
            child.status, child.completed_at, child.completed_by = P.SKIPPED, now, user
            child.save(update_fields=["status", "completed_at", "completed_by"])
    op.status = state
    op.completed_at = now if state != P.PENDING else None
    op.completed_by = user if state != P.PENDING else None
    op.save()
    _roll_up(op.parent, user)
    audit.record(
        "operation.reopen" if state == P.PENDING else f"operation.{state}",
        op.work_order,
        summary=f"{op.work_order.number}: “{op.name}” {before} → {state}",
    )
    return op


@transaction.atomic
def add_operation(
    wo: WorkOrder,
    *,
    name: str,
    description: str = "",
    machine=None,
    parent: Operation | None = None,
) -> Operation:
    _require_editable(wo)
    if parent is not None:
        if parent.work_order_id != wo.pk:
            raise ValidationError("That operation belongs to another work order.")
        if parent.depth + 1 > Operation.MAX_DEPTH:
            raise _too_deep()
    last = wo.operations.filter(parent=parent).aggregate(m=models.Max("position"))["m"] or 0
    op = Operation.objects.create(
        work_order=wo,
        parent=parent,
        depth=parent.depth + 1 if parent else 0,
        position=last + 10,
        name=name,
        description=description,
        machine=machine,
    )
    _roll_up(parent, None)
    where = f" under “{parent.name}”" if parent else ""
    audit.record("operation.add", wo, summary=f"Added operation “{name}”{where} to {wo.number}")
    return op


def parent_options(tree: list[Operation]) -> dict:
    """For each operation in `tree`, the ones it could be moved under: not itself,
    not one of its own sub-operations, and not so deep that its sub-operations
    would pass the limit. Worked out from the tree, without more queries."""
    kids: dict = {}
    for o in tree:
        kids.setdefault(o.parent_id, []).append(o)
    out = {}
    for op in tree:
        below, stack = [], [op]
        while stack:
            for child in kids.get(stack.pop().pk, []):
                below.append(child)
                stack.append(child)
        excluded = {op.pk, *(o.pk for o in below)}
        room = Operation.MAX_DEPTH - _height(op, below) - 1
        out[op.pk] = [o for o in tree if o.pk not in excluded and o.depth <= room]
    return out


@transaction.atomic
def reparent_operation(op: Operation, parent: Operation | None, *, user=None) -> Operation:
    """Move an operation, with its sub-operations, under another or to the top level."""
    wo = op.work_order
    _require_editable(wo)
    if parent is not None and parent.work_order_id != wo.pk:
        raise ValidationError("That operation belongs to another work order.")
    if op.parent_id == (parent.pk if parent else None):
        return op
    below = _descendants(op)
    if parent is not None and parent.pk in {op.pk, *(o.pk for o in below)}:
        raise ValidationError("An operation cannot go under itself or its own sub-operations.")
    new_depth = parent.depth + 1 if parent else 0
    if new_depth + _height(op, below) > Operation.MAX_DEPTH:
        raise _too_deep()
    old_parent, shift = op.parent, new_depth - op.depth
    last = wo.operations.filter(parent=parent).aggregate(m=models.Max("position"))["m"] or 0
    op.parent, op.depth, op.position = parent, new_depth, last + 10
    op.save(update_fields=["parent", "depth", "position"])
    for o in below:
        o.depth += shift
        o.save(update_fields=["depth"])
    _roll_up(old_parent, user)
    _roll_up(parent, user)
    where = f"under “{parent.name}”" if parent else "to the top level"
    audit.record("operation.move", wo, summary=f"{wo.number}: moved “{op.name}” {where}")
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
    """Remove an operation and its sub-operations. Time logged against them
    stays on the work order; returns how many time entries that is."""
    wo = op.work_order
    _require_editable(wo)
    below = _descendants(op)
    detached = sum(o.time_entries.count() for o in [op, *below])
    name, parent = op.name, op.parent
    op.delete()
    _roll_up(parent, None)
    extra = f" and {len(below)} sub-operation{'s' if len(below) != 1 else ''}" if below else ""
    audit.record("operation.delete", wo, summary=f"{wo.number}: deleted operation “{name}”{extra}")
    return detached


@transaction.atomic
def move_operation(op: Operation, direction: int) -> None:
    """Swap an operation with its neighbor above (-1) or below (+1) among its siblings."""
    wo = op.work_order
    _require_editable(wo)
    ops = list(
        wo.operations.select_for_update().filter(parent_id=op.parent_id).order_by("position", "id")
    )
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
    lot=None,
    leftover: tuple | None = None,
    user=None,
) -> list[MaterialUsage]:
    """Material consumed on a work order (FR-STK-8). A negative quantity corrects.

    A material tracked in stock is drawn from its lots by the stock module,
    which may write one usage per lot; anything else is costed from the price
    list, or at the cost given. Returns the usages written.
    """
    _require_editable(wo)
    if not quantity:
        raise ValidationError("Enter a quantity.")
    source = MaterialUsage.Source.MANUAL
    if material is not None:
        unit = unit or material.unit
        description = description or str(material)
        handled = hooks.handle_material(
            wo,
            material=material,
            quantity=quantity,
            unit=unit,
            description=description,
            unit_cost=unit_cost,
            note=note,
            billable=billable,
            lot=lot,
            leftover=leftover,
            user=user,
        )
        if handled is not None:
            return handled
        if leftover:
            raise ValidationError(
                f"{material} is not kept in stock, so a leftover piece cannot be kept."
            )
        if lot is not None:
            raise ValidationError(f"{material} is not tracked in stock, so it has no lots.")
        if unit_cost is None:
            source = MaterialUsage.Source.PRICE_LIST
            unit_cost = material.cost_per(unit)
    if unit_cost is None or not description:
        raise ValidationError("Pick a material, or describe it and give its cost.")
    return [
        write_usage(
            wo,
            material=material,
            description=description,
            quantity=quantity,
            unit=unit,
            unit_cost=unit_cost,
            note=note,
            billable=billable,
            source=source,
            user=user,
        )
    ]


def write_usage(
    wo: WorkOrder,
    *,
    material: Material | None,
    description: str,
    quantity: Decimal,
    unit: str,
    unit_cost: Decimal,
    total_cost: Decimal | None = None,
    note: str = "",
    billable: bool = True,
    source: str = MaterialUsage.Source.MANUAL,
    user=None,
) -> MaterialUsage:
    """Write one usage row. `total_cost` is given when it is known exactly (a lot
    draw); otherwise it is quantity × unit cost, rounded."""
    ensure_started(wo)
    usage = MaterialUsage.objects.create(
        work_order=wo,
        material=material,
        description=description,
        quantity=quantity,
        unit=unit,
        unit_cost=unit_cost,
        total_cost=round_money(quantity * unit_cost) if total_cost is None else total_cost,
        note=note,
        billable=billable,
        source=source,
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
