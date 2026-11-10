"""Work orders, operations, materials, notes and change orders (FR-WO-1..8)."""

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
            guards=(_operations_done, hooks.guard_for(Status.COMPLETE)),
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

    customer_scope_path = "work_order__job__customer_id"

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.text[:60]


# ---------------------------------------------------------------------------
# Change orders (FR-WO-5, FR-AGR-6)
# ---------------------------------------------------------------------------


class COStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SENT = "sent", "Sent"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"


def _co_has_lines(co):
    if not co.lines.exists() and not co.amended_terms.strip():
        yield "Add a line or amended terms before sending."


CO_LIFECYCLE = Lifecycle(
    field="status",
    initial=COStatus.DRAFT,
    transitions=[
        Transition(
            COStatus.DRAFT,
            COStatus.SENT,
            permission="workorders.edit",
            label="Mark as sent",
            guards=(_co_has_lines,),
        ),
        Transition(
            COStatus.SENT, COStatus.APPROVED, permission="workorders.approve", label="Approve"
        ),
        Transition(
            COStatus.SENT, COStatus.DECLINED, permission="workorders.edit", label="Declined"
        ),
    ],
)


class ChangeOrder(LifecycleModel, BaseModel):
    job = models.ForeignKey("jobs.Job", on_delete=models.PROTECT, related_name="change_orders")
    number = models.CharField(max_length=32, unique=True, editable=False)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    amended_terms = models.TextField(
        blank=True,
        help_text="Changed terms the customer accepts with this change order (optional).",
    )
    status = models.CharField(
        max_length=10, choices=COStatus.choices, default=COStatus.DRAFT, editable=False
    )
    total = MoneyField(default=ZERO, editable=False)
    approved_by_name = models.CharField(max_length=150, blank=True, editable=False)
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    approval_how = models.CharField(max_length=12, blank=True, editable=False)
    agreement = models.ForeignKey(
        "agreements.CustomerAgreement",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
    )

    lifecycle = CO_LIFECYCLE
    customer_scope_path = "job__customer_id"

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.number} {self.title}"

    def get_absolute_url(self) -> str:
        return reverse("workorders:change_order", args=[self.pk])


class ChangeOrderLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    change_order = models.ForeignKey(ChangeOrder, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=300)
    quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("1"))
    unit = models.CharField(max_length=12, choices=units.UNIT_CHOICES, default="each")
    unit_price = RateField(default=ZERO)
    unit_cost = RateField(default=ZERO)
    line_total = MoneyField(default=ZERO)
    category = models.CharField(
        max_length=10,
        default="other",
        choices=[
            ("materials", "Materials"),
            ("labor", "Labor"),
            ("machine", "Machine time"),
            ("other", "Other"),
        ],
    )
    tax_category = models.ForeignKey(
        "tax.TaxCategory", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    invoice_line_id = models.UUIDField(null=True, blank=True, editable=False)

    def __str__(self) -> str:
        return self.description
