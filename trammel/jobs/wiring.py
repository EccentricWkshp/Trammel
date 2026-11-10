import datetime as dt

from django.db.models import Case, F, IntegerField, Q, Value, When
from django.urls import reverse
from django.utils import timezone

from trammel.core import access, dashboard, panels

from .models import OPEN_STATES, Job, Priority, Status


@panels.register("customers.customer")
def customer_jobs(request, customer):
    if not access.has_perm(request.actor, "jobs.view"):
        return None
    jobs = access.scoped(request.actor, "jobs.view", Job.objects.filter(customer=customer))
    return panels.Panel(
        "Jobs",
        "jobs/_customer_panel.html",
        {"jobs": jobs[:50]},
        order=10,
        tab="jobs",
        count=jobs.count(),
    )


@dashboard.register(
    "jobs.priority",
    "Priority jobs",
    order=10,
    permission="jobs.view",
    description="High and rush jobs, and jobs due soon or past due.",
)
def priority_jobs(request):
    today = timezone.localdate()
    soon = today + dt.timedelta(days=Job.AT_RISK_DAYS)
    qs = access.scoped(
        request.actor,
        "jobs.view",
        Job.objects.filter(status__in=OPEN_STATES)
        .exclude(status=Status.ON_HOLD)
        .filter(
            Q(priority__gte=Priority.HIGH)
            | Q(due_date__lte=soon)
            | Q(promised_date__lt=today)
            | Q(target_completion__lt=today)
        ),
    )
    late = Q(due_date__lt=today) | Q(promised_date__lt=today)
    ordered = qs.select_related("customer").order_by(
        Case(When(late, then=Value(0)), default=Value(1), output_field=IntegerField()),
        "-priority",
        F("due_date").asc(nulls_last=True),
        "created_at",
    )
    rows = [(job, job.schedule_flags(today)) for job in ordered[: dashboard.LIMIT]]
    return dashboard.Section(
        template="jobs/_home_jobs.html",
        rows=rows,
        total=qs.count() if len(rows) == dashboard.LIMIT else None,
        more_url=reverse("jobs:list") + "?sort=priority",
        empty="No rush, high-priority or late jobs.",
    )


@dashboard.register(
    "jobs.on_hold",
    "Jobs on hold",
    order=20,
    permission="jobs.view",
    description="Jobs paused, with what they were paused from.",
)
def jobs_on_hold(request):
    today = timezone.localdate()
    qs = access.scoped(request.actor, "jobs.view", Job.objects.filter(status=Status.ON_HOLD))
    ordered = qs.select_related("customer").order_by(F("due_date").asc(nulls_last=True))
    rows = [(job, job.schedule_flags(today)) for job in ordered[: dashboard.LIMIT]]
    return dashboard.Section(
        template="jobs/_home_jobs.html",
        rows=rows,
        total=qs.count() if len(rows) == dashboard.LIMIT else None,
        more_url=reverse("jobs:list") + "?status=on_hold",
        empty="No jobs on hold.",
    )
