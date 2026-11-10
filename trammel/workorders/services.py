"""Work order writes (FR-WO). Web, API and the shop-floor app call these."""

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
    ACTIVE,
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
        # The quote's description says in words what the lines list.
        instructions="\n\n".join(
            t for t in (job.description, revision.description if revision else "") if t
        ),
    )
    save_audited(wo, summary=f"Created work order for {job.number}")
    if revision is not None:
        _carry_over(wo, revision)
    # Change orders approved before the work order existed come with it.
    for change in pending_change_orders(job):
        add_change_order(wo, change)
    return wo


def _carry_over(wo: WorkOrder, revision, parent: Operation | None = None) -> None:
    """An operation per line of `revision`, with its planned materials; under
    `parent` when given (a change order's lines are steps of one operation)."""
    position = 0
    for line in revision.lines.select_related("machine", "material"):
        if line.line_total < 0:
            continue  # a credit for removed scope is not work to do
        position += 10
        machine = line.machine
        if machine is None and line.inputs.get("machine"):
            machine = Machine.objects.filter(pk=line.inputs["machine"]).first()
        op = Operation.objects.create(
            work_order=wo,
            parent=parent,
            depth=parent.depth + 1 if parent else 0,
            position=position,
            name=line.description[:200],
            machine=machine,
        )
        # Other modules plan for the line too: stock expands a product's bill of materials.
        hooks.run_carry_over(wo, line, op)
        if line.kind == "material" and line.material is not None:
            PlannedMaterial.objects.create(
                work_order=wo,
                operation=op,
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
                operation=op,
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
                operation=op,
                material=material,
                description=row["detail"] if row else str(material),
            )


def _change_orders(job):
    from trammel.quotes.models import Quote, QuoteRevision

    return QuoteRevision.objects.filter(
        quote__job=job, quote__kind=Quote.Kind.CHANGE_ORDER, status="approved"
    ).select_related("quote")


def pending_change_orders(job) -> list:
    """The job's approved change orders not yet added to any of its work orders."""
    added = set(
        Operation.objects.filter(work_order__job=job, source_revision__isnull=False).values_list(
            "source_revision_id", flat=True
        )
    )
    return [r for r in _change_orders(job).order_by("quote__number") if r.pk not in added]


def change_orders_on(wo: WorkOrder) -> list:
    """The approved change orders added to this work order."""
    ids = wo.operations.filter(source_revision__isnull=False).values_list(
        "source_revision_id", flat=True
    )
    return list(_change_orders(wo.job).filter(pk__in=ids).order_by("quote__number"))


@transaction.atomic
def add_change_order(wo: WorkOrder, revision) -> Operation:
    """Add an approved change order to the work: one operation named for it, its
    lines as sub-operations with their planned materials and bills of materials,
    as a work order made from a quote gets them (FR-WO-5)."""
    _require_editable(wo)
    quote = revision.quote
    if not quote.is_change_order or revision.status != "approved" or quote.job_id != wo.job_id:
        raise ValidationError("Only an approved change order for this job can be added.")
    if Operation.objects.filter(work_order__job=wo.job, source_revision=revision).exists():
        raise ValidationError(f"{quote.number} has already been added to a work order.")
    last = wo.operations.filter(parent=None).aggregate(m=models.Max("position"))["m"] or 0
    op = Operation.objects.create(
        work_order=wo,
        position=last + 10,
        name=f"{quote.number}: {quote.title}"[:200],
        description=revision.description or revision.customer_notes,
        source_revision=revision,
    )
    _carry_over(wo, revision, parent=op)
    if not op.children.exists():  # nothing to make: a credit, say
        op.status = Operation.State.DONE
        op.completed_at = timezone.now()
        op.save(update_fields=["status", "completed_at"])
    audit.record("operation.add", wo, summary=f"Added change order {quote.number} to {wo.number}")
    return op


def add_approved_change_order(revision, approval=None) -> None:
    """On approval: straight onto the job's work order when it has exactly one
    active; otherwise each active work order offers it to be added by hand."""
    if not revision.quote.is_change_order:
        return
    active = list(revision.quote.job.work_orders.filter(status__in=ACTIVE)[:2])
    if len(active) == 1:
        add_change_order(active[0], revision)


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
        if state == P.DONE:
            hooks.run_operation_done(op, user)
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
    if state == P.DONE:
        hooks.run_operation_done(op, user)
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


def ready_to_record(wo: WorkOrder):
    """Planned materials of done operations, with a quantity, not yet recorded."""
    return (
        wo.planned_materials.filter(
            used_at__isnull=True,
            operation__status=P.DONE,
            material__isnull=False,
            quantity__isnull=False,
        )
        .select_related("material", "operation")
        .order_by("operation__position", "description")
    )


@transaction.atomic
def use_planned(wo: WorkOrder, planned, *, user=None) -> list[MaterialUsage]:
    """Record planned materials as used, as planned (FR-STK-6). Each is recorded
    once; stock draws tracked materials from their lots."""
    _require_editable(wo)
    usages = []
    for p in planned:
        p = PlannedMaterial.objects.select_for_update().get(pk=p.pk, work_order=wo)
        if p.used_at is not None or p.material is None or not p.quantity:
            continue
        usages += record_material(
            wo,
            material=p.material,
            quantity=p.quantity,
            unit=p.unit or p.material.unit,
            note=f"As planned{f' for {p.operation.name}' if p.operation else ''}",
            user=user,
        )
        PlannedMaterial.objects.filter(pk=p.pk).update(used_at=timezone.now())
    return usages


@transaction.atomic
def confirm_materials(wo: WorkOrder, confirmed: bool = True) -> None:
    WorkOrder.objects.filter(pk=wo.pk).update(materials_confirmed=confirmed)
    wo.materials_confirmed = confirmed
    audit.record(
        "work_order.materials_confirmed",
        wo,
        summary=f"{wo.number}: material use {'confirmed' if confirmed else 'unconfirmed'}",
    )
