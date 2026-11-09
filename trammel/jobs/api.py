# No `from __future__ import annotations` here: list_jobs is wrapped by Ninja's
# paginate decorator, which resolves string annotations against Ninja's own
# module, where JobStatus does not exist, and the OpenAPI schema then fails.
import datetime as dt
from uuid import UUID

from ninja import ModelSchema, Router, Schema, Status
from ninja.pagination import LimitOffsetPagination, paginate

from trammel.core import access, audit
from trammel.customers.models import Customer

from . import services
from .models import Job
from .models import Status as JobStatus

router = Router(tags=["jobs"])


class JobOut(ModelSchema):
    customer_name: str

    class Meta:
        model = Job
        fields = [
            "id",
            "number",
            "title",
            "description",
            "customer",
            "tags",
            "status",
            "held_from",
            "target_completion",
            "due_date",
            "promised_date",
            "delivery_planned",
            "delivery_actual",
            "force_closed_at",
            "created_at",
            "updated_at",
        ]

    @staticmethod
    def resolve_customer_name(obj):
        return obj.customer.name


class JobIn(Schema):
    title: str
    description: str = ""
    customer: UUID | None = None
    tags: list[str] = []
    target_completion: dt.date | None = None
    due_date: dt.date | None = None
    promised_date: dt.date | None = None
    delivery_planned: dt.date | None = None
    delivery_actual: dt.date | None = None


class JobPatch(Schema):
    title: str | None = None
    description: str | None = None
    customer: UUID | None = None
    tags: list[str] | None = None
    target_completion: dt.date | None = None
    due_date: dt.date | None = None
    promised_date: dt.date | None = None
    delivery_planned: dt.date | None = None
    delivery_actual: dt.date | None = None


class JobTransitionIn(Schema):
    target: JobStatus
    reason: str = ""


class OutstandingOut(Schema):
    key: str
    label: str
    choices: list[tuple[str, str]]


class ForceCloseIn(Schema):
    reason: str
    resolutions: dict[str, str] = {}


def _customer(request, customer_id):
    if customer_id is None:
        return None
    return access.get_or_404(
        request.actor, "customers.view", Customer.objects.all(), pk=customer_id
    )


def _job(request, perm, job_id):
    return access.get_or_404(request.actor, perm, Job.objects.select_related("customer"), pk=job_id)


@router.get("/", response=list[JobOut])
@paginate(LimitOffsetPagination)
def list_jobs(request, status: JobStatus | None = None, customer: UUID | None = None):
    qs = Job.objects.select_related("customer")
    if status:
        qs = qs.filter(status=status)
    if customer:
        qs = qs.filter(customer_id=customer)
    return access.scoped(request.actor, "jobs.view", qs)


@router.get("/{job_id}", response=JobOut)
def get_job(request, job_id: UUID):
    return _job(request, "jobs.view", job_id)


@router.post("/", response={201: JobOut})
def create_job(request, payload: JobIn):
    access.require(request.actor, "jobs.create")
    data = payload.dict()
    data["customer"] = _customer(request, data.pop("customer"))
    return Status(201, services.create_job(Job(**data)))


@router.patch("/{job_id}", response=JobOut)
def update_job(request, job_id: UUID, payload: JobPatch):
    job = _job(request, "jobs.edit", job_id)
    before = audit.snapshot(job)
    data = payload.dict(exclude_unset=True)
    if "customer" in data:
        data["customer"] = _customer(request, data["customer"]) or Customer.placeholder()
    for field, value in data.items():
        setattr(job, field, value)
    return services.update_job(job, before=before)


@router.post("/{job_id}/transition", response=JobOut)
def transition_job(request, job_id: UUID, payload: JobTransitionIn):
    job = _job(request, "jobs.view", job_id)
    services.transition(job, payload.target, actor=request.actor, reason=payload.reason)
    job.refresh_from_db()
    return job


@router.get("/{job_id}/outstanding", response=list[OutstandingOut])
def job_outstanding(request, job_id: UUID):
    """What a forced closeout would have to resolve (§6.2)."""
    return services.outstanding(_job(request, "jobs.force_closeout", job_id))


@router.post("/{job_id}/force-close", response=JobOut)
def force_close_job(request, job_id: UUID, payload: ForceCloseIn):
    job = _job(request, "jobs.force_closeout", job_id)
    services.force_close(
        job, actor=request.actor, reason=payload.reason, resolutions=payload.resolutions
    )
    job.refresh_from_db()
    return job


@router.post("/{job_id}/duplicate", response={201: JobOut})
def duplicate_job(request, job_id: UUID):
    access.require(request.actor, "jobs.create")
    return Status(201, services.duplicate_job(_job(request, "jobs.view", job_id)))


@router.delete("/{job_id}", response={204: None})
def delete_job(request, job_id: UUID):
    services.delete_job(_job(request, "jobs.delete", job_id))
    return Status(204, None)
