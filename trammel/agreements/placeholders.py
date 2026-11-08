"""Agreement template placeholders and preview data."""

from __future__ import annotations

from decimal import Decimal

from django.utils import timezone

from trammel.core import placeholders as ph
from trammel.core.placeholders import P

AGREEMENT = ph.Group(
    "This agreement",
    (
        P("today", "The date it is issued"),
        P("payment_terms_days", "The customer's payment terms in days"),
        P("deposit_percent", "Deposit percentage, when issued with a quote that asks for one"),
    ),
)

JOB = ph.Group(
    "Job",
    (
        P("job.number", "Job number"),
        P("job.title", "Title"),
        P("job.description", "Description"),
        P("job.due_date", "Due date"),
        P("job.promised_date", "Date promised to the customer"),
    ),
)


def preview() -> tuple[dict, str]:
    from trammel.jobs.models import Job

    from .services import context_for

    job = Job.objects.select_related("customer").order_by("-created_at").first()
    if job is not None:
        return context_for(job, deposit_percent=Decimal("50")), f"job {job.number}"
    return {
        "business": ph.business(),
        "customer": ph.SAMPLE_CUSTOMER,
        "job": ph.SAMPLE_JOB,
        "payment_terms_days": 30,
        "today": timezone.localdate(),
        "deposit_percent": Decimal("50"),
    }, "sample data"


ph.register(
    ph.Kind(
        key="agreement",
        title="Customer agreements",
        groups=(AGREEMENT, JOB, ph.CUSTOMER, ph.BUSINESS),
        preview=preview,
        html=False,
        module="agreements",
        permission="agreements.manage",
        order=30,
    )
)
