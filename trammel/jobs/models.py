"""Jobs: the container for one piece of customer work (SPEC §5, §6, FR-JOB)."""

from __future__ import annotations

import datetime as dt

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.urls import reverse

from trammel.core.lifecycle import Lifecycle, LifecycleModel, Transition
from trammel.core.models import BaseModel

from . import hooks


class Status(models.TextChoices):
    PROPOSAL = "proposal", "Proposal"
    QUOTED = "quoted", "Quoted"
    APPROVED = "approved", "Approved"
    IN_PROGRESS = "in_progress", "In progress"
    COMPLETED = "completed", "Completed"
    CLOSED = "closed", "Closed"
    ON_HOLD = "on_hold", "On hold"
    DECLINED = "declined", "Declined"
    # The stored value is the SPEC §6.1 state name; the label is US English.
    CANCELLED = "cancelled", "Canceled"


#: States in which there is still work to do — used for schedule flags.
OPEN_STATES = frozenset(
    {Status.PROPOSAL, Status.QUOTED, Status.APPROVED, Status.IN_PROGRESS, Status.ON_HOLD}
)
#: States a job can be put on hold from, and resumed back to.
HOLDABLE = (Status.PROPOSAL, Status.QUOTED, Status.APPROVED, Status.IN_PROGRESS)
TERMINAL = frozenset({Status.CLOSED, Status.DECLINED, Status.CANCELLED})


def _remember_hold(job, transition, actor):
    # The source state is the one the job was in a moment ago.
    previous = job.history().order_by("-at").values_list("from_state", flat=True).first()
    type(job).all_objects.filter(pk=job.pk).update(held_from=previous or "")
    job.held_from = previous or ""


def _clear_hold(job, transition, actor):
    type(job).all_objects.filter(pk=job.pk).update(held_from="")
    job.held_from = ""


def _mark_force_closed(job, transition, actor):
    from django.utils import timezone

    now = timezone.now()
    type(job).all_objects.filter(pk=job.pk).update(force_closed_at=now)
    job.force_closed_at = now


def _resume_to(state):
    return lambda job: job.held_from == state


def _is_internal(job) -> bool:
    return job.customer.is_internal


_FWD = "jobs.edit"

JOB_LIFECYCLE = Lifecycle(
    field="status",
    initial=Status.PROPOSAL,
    transitions=[
        # Forward path (§6.1). Each step asks the modules that care (hooks.py).
        Transition(
            Status.PROPOSAL,
            Status.QUOTED,
            permission=_FWD,
            label="Mark quoted",
            guards=(hooks.guard_for(Status.QUOTED),),
        ),
        # Work for the shop itself has no customer to approve a quote, so it
        # is approved directly. Listed first: from quoted it wins over the
        # quote-approval step below.
        Transition(
            (Status.PROPOSAL, Status.QUOTED),
            Status.APPROVED,
            permission=_FWD,
            label="Approve internal job",
            when=_is_internal,
        ),
        Transition(
            Status.QUOTED,
            Status.APPROVED,
            permission=_FWD,
            label="Mark approved",
            guards=(hooks.guard_for(Status.APPROVED),),
        ),
        Transition(
            Status.APPROVED,
            Status.IN_PROGRESS,
            permission=_FWD,
            label="Start work",
            guards=(hooks.guard_for(Status.IN_PROGRESS),),
        ),
        Transition(
            Status.IN_PROGRESS,
            Status.COMPLETED,
            permission=_FWD,
            label="Mark completed",
            guards=(hooks.guard_for(Status.COMPLETED),),
        ),
        Transition(
            Status.COMPLETED,
            Status.CLOSED,
            permission=_FWD,
            label="Close out",
            guards=(hooks.guard_for(Status.CLOSED),),
        ),
        # Side states.
        Transition(
            HOLDABLE,
            Status.ON_HOLD,
            permission=_FWD,
            requires_reason=True,
            label="Put on hold",
            after=(_remember_hold,),
        ),
        *[
            Transition(
                Status.ON_HOLD,
                state,
                permission=_FWD,
                label="Resume",
                when=_resume_to(state),
                after=(_clear_hold,),
            )
            for state in HOLDABLE
        ],
        Transition(
            (Status.PROPOSAL, Status.QUOTED),
            Status.DECLINED,
            permission=_FWD,
            label="Customer declined",
        ),
        Transition(
            (*HOLDABLE, Status.ON_HOLD),
            Status.CANCELLED,
            permission=_FWD,
            requires_reason=True,
            label="Cancel job",
            after=(_clear_hold,),
        ),
        # One step back (§6.2).
        Transition(
            Status.COMPLETED,
            Status.IN_PROGRESS,
            kind="reopen",
            permission="jobs.reopen",
            label="Reopen",
        ),
        Transition(
            Status.CLOSED, Status.COMPLETED, kind="reopen", permission="jobs.reopen", label="Reopen"
        ),
        # Forced closeout (§6.2) — through services.force_close, which resolves
        # outstanding items first.
        Transition(
            (*HOLDABLE, Status.COMPLETED, Status.ON_HOLD),
            Status.CLOSED,
            kind="force",
            permission="jobs.force_closeout",
            label="Force closeout",
            after=(_clear_hold, _mark_force_closed),
        ),
    ],
)


class Priority(models.IntegerChoices):
    """Stored as a number so lists sort by it; the API uses the lower-case names."""

    LOW = 0, "Low"
    NORMAL = 1, "Normal"
    HIGH = 2, "High"
    RUSH = 3, "Rush"

    @property
    def key(self) -> str:
        return self.name.lower()


class Job(LifecycleModel, BaseModel):
    number = models.CharField(max_length=32, unique=True, editable=False)
    title = models.CharField(max_length=200)
    priority = models.PositiveSmallIntegerField(
        choices=Priority.choices,
        default=Priority.NORMAL,
        db_index=True,
        help_text="High and rush jobs are listed first on the home screen.",
    )
    description = models.TextField(blank=True, help_text="Scope of work.")
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="jobs"
    )
    tags = ArrayField(models.CharField(max_length=40), default=list, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PROPOSAL, editable=False
    )
    held_from = models.CharField(max_length=16, blank=True, editable=False)

    # Job dates (FR-JOB-8). Each optional; edits are kept in the audit log.
    target_completion = models.DateField(
        null=True, blank=True, help_text="The shop's internal goal."
    )
    due_date = models.DateField(
        "due", null=True, blank=True, help_text="When the customer needs it."
    )
    promised_date = models.DateField(
        "promised", null=True, blank=True, help_text="What the shop committed to."
    )
    delivery_planned = models.DateField("delivery / pickup (planned)", null=True, blank=True)
    delivery_actual = models.DateField("delivery / pickup (actual)", null=True, blank=True)

    force_closed_at = models.DateTimeField(null=True, blank=True, editable=False)

    lifecycle = JOB_LIFECYCLE
    customer_scope_path = "customer_id"

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "due_date"]),
            models.Index(fields=["customer", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.number} {self.title}"

    def get_absolute_url(self) -> str:
        return reverse("jobs:detail", args=[self.pk])

    @property
    def held_from_label(self) -> str:
        return Status(self.held_from).label if self.held_from else ""

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATES

    @property
    def is_priority(self) -> bool:
        return self.priority >= Priority.HIGH

    @property
    def priority_key(self) -> str:
        return Priority(self.priority).key

    @property
    def is_deletable(self) -> bool:
        return self.status in {Status.PROPOSAL, Status.DECLINED, Status.CANCELLED}

    DATE_FIELDS = (
        ("target_completion", "Target"),
        ("due_date", "Due"),
        ("promised_date", "Promised"),
        ("delivery_planned", "Delivery"),
    )

    #: "Due soon" window for the at-risk flag (FR-JOB-9), in days.
    AT_RISK_DAYS = 3

    def schedule_flags(self, today: dt.date) -> list[tuple[str, str]]:
        """(level, message) late / at-risk indicators for open jobs (FR-JOB-9)."""
        if not self.is_open:
            return []
        from trammel.core.formats import show

        flags = []
        if self.promised_date and self.promised_date < today:
            flags.append(("late", f"Promised date passed ({show(self.promised_date)})"))
        if self.target_completion and self.target_completion < today:
            flags.append(("late", f"Target date passed ({show(self.target_completion)})"))
        if self.due_date:
            if self.due_date < today:
                flags.append(("late", f"Past due ({show(self.due_date)})"))
            elif (self.due_date - today).days <= self.AT_RISK_DAYS:
                flags.append(("risk", f"Due soon ({show(self.due_date)})"))
        return flags
