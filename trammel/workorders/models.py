"""Work orders, operations, materials and notes (FR-WO-1..8).

Change orders are quotes of their own kind (trammel.quotes); an approved one
adds its operations here (services.add_change_order)."""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.urls import reverse

from trammel.core import units
from trammel.core.access import SYSTEM
from trammel.core.lifecycle import Lifecycle, LifecycleModel, Transition
from trammel.core.models import AppendOnlyModel, BaseModel, uuid7
from trammel.core.money import MoneyField, RateField

from . import hooks

ZERO = Decimal("0")


class Status(models.TextChoices):
    OPEN = "open", "Open"
    IN_PROGRESS = "in_progress", "In progress"
    WAITING = "waiting", "Waiting"
    COMPLETE = "complete", "Complete"
    CLOSED = "closed", "Closed"
    CANCELLED = "cancelled", "Canceled"


def _operations_done(wo):
    pending = wo.operations.filter(status=Operation.State.PENDING).count()
    if pending:
        yield f"{pending} operation(s) not done or skipped."


def _items_included(wo):
    pending = wo.items.filter(status=IncludedItem.State.PENDING).count()
    if pending:
        yield f"{pending} item(s) to include not checked off."


def _materials_confirmed(wo):
    if not wo.materials_confirmed:
        yield "Confirm that all material use has been recorded."


def _agreement(wo):
    from trammel.agreements import services as agreements

    if not agreements.satisfied(wo.job):
        yield "The customer agreement has not been accepted (or waived) for this job."


def _start_job(wo, transition, actor):
    """Starting work on a work order starts the job (§6 step 7)."""
    from trammel.jobs.models import Job
    from trammel.jobs.models import Status as JobStatus

    job = Job.objects.get(pk=wo.job_id)
    if job.status == JobStatus.APPROVED:
        Job.lifecycle.apply(job, JobStatus.IN_PROGRESS, actor=SYSTEM)


def _reopen_job(wo, transition, actor):
    """§6.2: reopening a work order in a completed or closed job reopens the job too."""
    from trammel.jobs.models import Job
    from trammel.jobs.models import Status as JobStatus

    job = Job.objects.get(pk=wo.job_id)
    reason = f"Work order {wo.number} reopened"
    if job.status == JobStatus.CLOSED:
        Job.lifecycle.apply(job, JobStatus.COMPLETED, actor=SYSTEM, kind="reopen", reason=reason)
        job.refresh_from_db()
    if job.status == JobStatus.COMPLETED:
        Job.lifecycle.apply(job, JobStatus.IN_PROGRESS, actor=SYSTEM, kind="reopen", reason=reason)


ACTIVE = (Status.OPEN, Status.IN_PROGRESS, Status.WAITING)

WO_LIFECYCLE = Lifecycle(
    field="status",
    initial=Status.OPEN,
    transitions=[
        Transition(
            Status.OPEN,
            Status.IN_PROGRESS,
            permission="workorders.edit",
            label="Start work",
            guards=(_agreement,),
            after=(_start_job,),
        ),
        Transition(
            Status.IN_PROGRESS,
            Status.WAITING,
            permission="workorders.edit",
            requires_reason=True,
            label="Waiting…",
        ),
        Transition(
            Status.WAITING, Status.IN_PROGRESS, permission="workorders.edit", label="Resume"
        ),
        Transition(
            Status.IN_PROGRESS,
            Status.COMPLETE,
            permission="workorders.edit",
            label="Mark complete",
            guards=(_operations_done, _items_included, hooks.guard_for(Status.COMPLETE)),
        ),
        Transition(
            Status.COMPLETE,
            Status.CLOSED,
            permission="workorders.edit",
            label="Close out",
            guards=(_materials_confirmed, hooks.guard_for(Status.CLOSED)),
        ),
        Transition(
            ACTIVE,
            Status.CANCELLED,
            permission="workorders.edit",
            requires_reason=True,
            label="Cancel",
        ),
        Transition(
            Status.COMPLETE,
            Status.IN_PROGRESS,
            kind="reopen",
            permission="workorders.reopen",
            label="Reopen",
            after=(_reopen_job,),
        ),
        Transition(
            Status.CLOSED,
            Status.COMPLETE,
            kind="reopen",
            permission="workorders.reopen",
            label="Reopen",
            after=(_reopen_job,),
        ),
        Transition(
            Status.CANCELLED,
            Status.OPEN,
            kind="reopen",
            permission="workorders.reopen",
            label="Reopen",
        ),
    ],
)


class WorkOrder(LifecycleModel, BaseModel):
    class WaitingOn(models.TextChoices):
        MATERIALS = "materials", "Materials"
        CUSTOMER = "customer", "Customer"
        OTHER = "other", "Other"

    job = models.ForeignKey("jobs.Job", on_delete=models.PROTECT, related_name="work_orders")
    number = models.CharField(max_length=32, unique=True, editable=False)
    title = models.CharField(max_length=200)
    instructions = models.TextField(blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.OPEN, editable=False
    )
    waiting_on = models.CharField(
        max_length=10, choices=WaitingOn.choices, blank=True, editable=False
    )
    target_completion = models.DateField(null=True, blank=True)
    #: Moved in the schedule's queue (0.8.1): just before or after another work
    #: order. Moves apply in the order they were made, on top of the queue's own
    #: order; one whose other work order has left the queue no longer applies.
    queue_anchor = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    queue_side = models.CharField(max_length=6, blank=True)  # "before" or "after"
    queue_moved_at = models.DateTimeField(null=True, blank=True)
    source_revision = models.ForeignKey(
        "quotes.QuoteRevision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="work_orders",
    )
    materials_confirmed = models.BooleanField(default=False, editable=False)

    lifecycle = WO_LIFECYCLE
    customer_scope_path = "job__customer_id"

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.number} {self.title}"

    def get_absolute_url(self) -> str:
        return reverse("workorders:detail", args=[self.pk])

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE

    @property
    def is_editable(self) -> bool:
        return self.status not in (Status.CLOSED, Status.CANCELLED)


class Operation(models.Model):
    class State(models.TextChoices):
        PENDING = "pending", "To do"
        DONE = "done", "Done"
        SKIPPED = "skipped", "Skipped"

    #: Sub-operations go at most four levels deep: depth 0 to 3.
    MAX_DEPTH = 3

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="operations")
    #: The operation this is a step of. A parent is done when all of its
    #: sub-operations are done or skipped (services._roll_up).
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    depth = models.PositiveSmallIntegerField(default=0, editable=False)
    #: Order among siblings.
    position = models.PositiveIntegerField(default=0)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    machine = models.ForeignKey(
        "shop.Machine", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(max_length=8, choices=State.choices, default=State.PENDING)
    completed_at = models.DateTimeField(null=True, blank=True)
    #: Minutes planned for it, from a product's routing or typed in: hands-on
    #: time (setup included) and machine time. Time logged against the operation
    #: is shown beside them.
    planned_labor_minutes = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    planned_machine_minutes = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    #: Hours the work sits after it (glue, finish, paint) before the next can start.
    planned_wait_hours = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    #: Pinned on the schedule (0.8.1): planned from this day's opening, ahead of
    #: the queue; and kept on its machine rather than another in its group.
    pinned_date = models.DateField(null=True, blank=True)
    machine_pinned = models.BooleanField(default=False)
    #: When on its pinned day to start it (else the day's opening), and whether
    #: to work outside shop hours that day (early, late, or on a day off) to
    #: finish it, without changing the shop's hours.
    pinned_from = models.TimeField(null=True, blank=True)
    pinned_overtime = models.BooleanField(default=False)
    #: Set on the operation an approved change order added (its lines are the
    #: sub-operations), so a change order is never added twice.
    source_revision = models.ForeignKey(
        "quotes.QuoteRevision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    customer_scope_path = "work_order__job__customer_id"

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(depth__lte=3), name="operation_depth_limit")
        ]

    def __str__(self) -> str:
        return self.name


class PlannedMaterial(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    work_order = models.ForeignKey(
        WorkOrder, on_delete=models.CASCADE, related_name="planned_materials"
    )
    material = models.ForeignKey(
        "shop.Material", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    description = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    unit = models.CharField(max_length=12, choices=units.UNIT_CHOICES, blank=True)
    #: The operation it is for (the quote line it came from), so it can be recorded
    #: as used when that operation is done.
    operation = models.ForeignKey(
        "Operation", null=True, blank=True, on_delete=models.SET_NULL, related_name="planned"
    )
    #: When it was recorded as used, from the plan.
    used_at = models.DateTimeField(null=True, blank=True, editable=False)

    def __str__(self) -> str:
        return self.description


class IncludedItem(models.Model):
    """Something that goes with the job but isn't made: a resold product from the
    quote, such as a jar of board butter, to set aside or pack with the work. It
    is checked off, not worked on, so it isn't an operation."""

    class State(models.TextChoices):
        PENDING = "pending", "To include"
        DONE = "done", "Included"
        SKIPPED = "skipped", "Not needed"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(
        "catalog.Product", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    description = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("1"))
    unit = models.CharField(max_length=12, choices=units.UNIT_CHOICES, blank=True)
    position = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=8, choices=State.choices, default=State.PENDING)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The change order it came from, if not the work order's own quote.
    source_revision = models.ForeignKey(
        "quotes.QuoteRevision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
    )

    customer_scope_path = "work_order__job__customer_id"

    class Meta:
        ordering = ["position", "id"]
        verbose_name = "item to include"
        verbose_name_plural = "items to include"

    def __str__(self) -> str:
        return self.description


class MaterialUsage(AppendOnlyModel):
    """Material consumed on a work order (FR-STK-8).

    Append-only: a mistake is corrected with a negative entry. A material
    tracked in stock is drawn from its lots (FR-STK-3), one usage per lot, at
    the lot's cost; the stock ledger holds the per-lot detail.
    """

    class Source(models.TextChoices):
        PRICE_LIST = "price_list", "Price list"
        STOCK = "stock", "Stock"
        MANUAL = "manual", "Cost entered"

    work_order = models.ForeignKey(WorkOrder, on_delete=models.PROTECT, related_name="material_use")
    material = models.ForeignKey(
        "shop.Material", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    description = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit = models.CharField(max_length=12, choices=units.UNIT_CHOICES)
    unit_cost = RateField()
    total_cost = MoneyField()
    billable = models.BooleanField(default=True)
    note = models.CharField(max_length=300, blank=True)
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.PRICE_LIST)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Set by billing when the usage is pulled into an invoice (FR-BIL-1).
    invoice_line_id = models.UUIDField(null=True, blank=True)

    server_writable_fields = frozenset({"invoice_line_id"})
    customer_scope_path = "work_order__job__customer_id"

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.quantity} {self.unit} {self.description}"


class WorkOrderNote(AppendOnlyModel):
    """A note or photo from the office or the shop floor (FR-WO-6). Append-only."""

    work_order = models.ForeignKey(WorkOrder, on_delete=models.PROTECT, related_name="notes")
    text = models.TextField(blank=True)
    document = models.ForeignKey(
        "files.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Shown to the customer in the portal as an update on their job (FR-PRT-4).
    #: Nothing is, unless staff choose. Who sees a note is not part of what it
    #: says, so it can change on a note that otherwise never does.
    shared_with_customer = models.BooleanField(default=False)

    server_writable_fields = frozenset({"shared_with_customer"})
    customer_scope_path = "work_order__job__customer_id"

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.text[:60]
