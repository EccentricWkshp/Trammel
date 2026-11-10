from django.db.models import Case, Count, F, IntegerField, Q, Value, When
from django.urls import reverse

from trammel.core import access, dashboard, panels
from trammel.core.access import SYSTEM
from trammel.jobs import hooks

from . import hooks as hooks_wo
from .models import ACTIVE, Status, WorkOrder
from .models import Operation as _Op

OpStatus = _Op.State


@hooks.check("completed")
def work_orders_complete(job):
    open_wos = job.work_orders.filter(status__in=ACTIVE)
    for wo in open_wos:
        yield f"Work order {wo.number} is {wo.get_status_display().lower()}."


@hooks.check("closed")
def work_orders_closed(job):
    for wo in job.work_orders.exclude(status__in=[Status.CLOSED, Status.CANCELLED]):
        yield f"Work order {wo.number} is not closed."


@hooks.register_outstanding
def open_work_orders(job):
    """§6.2: a forced closeout cancels open work orders and closes complete ones."""
    for wo in job.work_orders.exclude(status__in=[Status.CLOSED, Status.CANCELLED]):
        if wo.status == Status.COMPLETE:

            def close(choice, wo=wo):
                WorkOrder.objects.filter(pk=wo.pk).update(materials_confirmed=True)
                wo.refresh_from_db()
                WorkOrder.lifecycle.apply(wo, Status.CLOSED, actor=SYSTEM)

            yield hooks.Outstanding(
                key=f"work_order:{wo.pk}",
                label=f"Work order {wo.number} is complete, not closed",
                choices=[("close", "Close it")],
                resolve=close,
                detail={"work_order": wo.number, "status": wo.status},
            )
        else:

            def cancel(choice, wo=wo):
                WorkOrder.lifecycle.apply(
                    wo, Status.CANCELLED, actor=SYSTEM, reason="Job force-closed"
                )

            yield hooks.Outstanding(
                key=f"work_order:{wo.pk}",
                label=f"Work order {wo.number} is {wo.get_status_display().lower()}",
                choices=[("cancel", "Cancel it")],
                resolve=cancel,
                detail={"work_order": wo.number, "status": wo.status},
            )


@panels.register("jobs.job")
def job_work_orders(request, job):
    if not access.has_perm(request.actor, "workorders.view"):
        return None
    from trammel.quotes.models import QuoteRevision

    from .services import can_create_for

    return panels.Panel(
        "Work orders",
        "workorders/_job_panel.html",
        {
            "work_orders": access.scoped(
                request.actor, "workorders.view", job.work_orders.all()
            ).order_by("created_at"),
            "approved": QuoteRevision.objects.filter(
                quote__job=job, quote__kind="quote", status="approved"
            ).select_related("quote"),
            "can_create": access.has_perm(request.actor, "workorders.create", job),
            "create_problems": can_create_for(job),
        },
        order=20,
        tab="work-orders",
        count=job.work_orders.count(),
    )


@dashboard.register(
    "workorders.active",
    "Open work orders",
    order=40,
    permission="workorders.view",
    description="Work orders not yet complete, waiting ones first.",
)
def active_work_orders(request):
    qs = access.scoped(
        request.actor, "workorders.view", WorkOrder.objects.filter(status__in=ACTIVE)
    )
    ordered = qs.select_related("job__customer").order_by(
        Case(
            When(status=Status.WAITING, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        ),
        F("target_completion").asc(nulls_last=True),
        "created_at",
    )
    rows = list(ordered[: dashboard.LIMIT])
    return dashboard.Section(
        template="workorders/_home_active.html",
        rows=rows,
        total=qs.count() if len(rows) == dashboard.LIMIT else None,
        more_url=reverse("workorders:list"),
        empty="No open work orders.",
    )


@dashboard.register(
    "workorders.to_record",
    "Materials waiting to be recorded",
    order=45,
    permission="workorders.edit",
    description="Work orders with operations done whose planned materials aren't recorded yet.",
)
def materials_to_record(request):
    if hooks_wo.planned_use_mode() == "off":
        return None
    waiting = Q(
        planned_materials__used_at__isnull=True,
        planned_materials__operation__status=OpStatus.DONE,
        planned_materials__material__isnull=False,
        planned_materials__quantity__isnull=False,
    )
    qs = (
        access.scoped(request.actor, "workorders.edit", WorkOrder.objects.filter(status__in=ACTIVE))
        .annotate(waiting=Count("planned_materials", filter=waiting))
        .filter(waiting__gt=0)
    )
    rows = list(qs.select_related("job__customer").order_by("number")[: dashboard.LIMIT])
    return dashboard.Section(
        template="workorders/_home_to_record.html",
        rows=rows,
        total=qs.count() if len(rows) == dashboard.LIMIT else None,
        more_url=reverse("workorders:list"),
        empty="Every done operation's materials are recorded.",
    )


@panels.register("workorders.workorder")
def work_order_change_orders(request, wo):
    """The job's approved change orders: those on this work order, and those not
    on any yet, to add (FR-WO-5)."""
    from trammel.core import modules

    from . import services

    if not modules.is_enabled("quotes") or not access.has_perm(request.actor, "quotes.view"):
        return None
    added = services.change_orders_on(wo)
    pending = services.pending_change_orders(wo.job)
    if not added and not pending:
        return None
    return panels.Panel(
        "Change orders",
        "workorders/_change_orders.html",
        {
            "wo": wo,
            "added": added,
            "pending": pending,
            "can_add": wo.status in ACTIVE
            and access.has_perm(request.actor, "workorders.edit", wo),
        },
        order=30,
        tab="change-orders",
        count=len(added) + len(pending),
    )


def _change_order_approved(revision, approval=None):
    from trammel.core import modules

    from . import services

    if modules.is_enabled("workorders"):
        services.add_approved_change_order(revision, approval)


def _connect():
    from trammel.quotes import services as quotes

    quotes.on_approved.append(_change_order_approved)


_connect()
