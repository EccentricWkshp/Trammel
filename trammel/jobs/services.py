"""Job writes (FR-JOB). Web views and the API both go through these."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from trammel.core import access, audit, numbering
from trammel.core.lifecycle import TransitionError
from trammel.core.services import save_audited
from trammel.customers.models import Customer

from . import hooks
from .models import Job, Status


@transaction.atomic
def create_job(job: Job) -> Job:
    """Create a job in the proposal stage, numbered (FR-JOB-1)."""
    if job.customer_id is None:
        job.customer = Customer.placeholder()
    job.tags = normalize_tags(job.tags)
    job.number = numbering.next_number("job")
    return save_audited(job, summary=f"Created job {job.number}")


@transaction.atomic
def update_job(job: Job, *, before: dict) -> Job:
    if job.status in {Status.CLOSED, Status.DECLINED, Status.CANCELLED}:
        raise ValidationError(
            "A closed, declined or canceled job cannot be edited. Reopen it first."
        )
    job.tags = normalize_tags(job.tags)
    old_customer = before.get("customer")
    save_audited(job, before=before)
    hooks.run_updated(job, before)
    if old_customer and old_customer != str(job.customer_id):
        old = Customer.all_objects.filter(pk=old_customer).first()
        if old is not None and old.is_placeholder:
            # §6 step 2: the placeholder is replaced; the history stays on the job.
            audit.record(
                "job.customer_replaced",
                job,
                summary=f"Replaced placeholder customer on {job.number} with {job.customer}",
            )
    return job


@transaction.atomic
def duplicate_job(source: Job) -> Job:
    """Copy scope for repeat work (FR-JOB-5). Not files, time, money, or dates."""
    copy = Job(
        title=source.title,
        description=source.description,
        customer=source.customer,
        tags=list(source.tags),
    )
    create_job(copy)
    hooks.run_duplicate(source, copy)
    audit.record(
        "job.duplicate",
        copy,
        summary=f"Duplicated {source.number} as {copy.number}",
        after={"source": source.number},
    )
    return copy


@transaction.atomic
def delete_job(job: Job, *, reason: str = "") -> None:
    if not job.is_deletable:
        raise ValidationError(
            "Only proposal, declined or canceled jobs can be deleted. Cancel it first."
        )
    job.delete()
    audit.record_delete(job, reason=reason)


def transition(job: Job, target: str, *, actor, reason: str = "") -> None:
    """A normal lifecycle move: forward, hold, resume, decline, cancel or reopen."""
    job.refresh_from_db(fields=["status", "held_from"])
    t = Job.lifecycle.find(job, target)
    if t is None or t.kind == "force":
        raise TransitionError([f"Cannot move from “{job.get_status_display()}” to “{target}”."])
    Job.lifecycle.apply(job, target, actor=actor, reason=reason, kind=t.kind)


def outstanding(job: Job) -> list[hooks.Outstanding]:
    return hooks.outstanding(job)


@transaction.atomic
def force_close(job: Job, *, actor, reason: str, resolutions: dict[str, str]) -> None:
    """Close a job despite failing checks (§6.2, FR-JOB-7).

    Every outstanding item is resolved by the admin's choice (or its default)
    before the job closes, and a snapshot of the outstanding state is stored on
    the state change and in the audit log.
    """
    items = outstanding(job)
    snapshot = []
    for item in items:
        allowed = [value for value, _label in item.choices]
        choice = resolutions.get(item.key) or allowed[0]
        if choice not in allowed:
            raise ValidationError(f"“{choice}” is not a valid resolution for {item.label}.")
        snapshot.append({"key": item.key, "label": item.label, "resolution": choice, **item.detail})
    # Check permission and reason before resolving anything, so a refusal
    # leaves work orders and invoices untouched. apply() re-checks both.
    t = Job.lifecycle.find(job, Status.CLOSED, "force")
    if t is None:
        raise TransitionError([f"A {job.get_status_display().lower()} job cannot be force-closed."])
    if not reason.strip():
        raise TransitionError(["A reason is required."])
    if not access.has_perm(actor, t.permission, job):
        raise PermissionDenied(f"Requires the {t.permission} permission.")
    for item, entry in zip(items, snapshot, strict=True):
        item.resolve(entry["resolution"])
    unresolved = hooks.problems(job, Status.CLOSED)
    Job.lifecycle.apply(
        job,
        Status.CLOSED,
        actor=actor,
        reason=reason,
        kind="force",
        details={"outstanding": snapshot, "checks_failing": unresolved},
    )


def normalize_tags(tags) -> list[str]:
    seen = []
    for tag in tags or []:
        tag = tag.strip().lower()
        if tag and tag not in seen:
            seen.append(tag[:40])
    return seen
