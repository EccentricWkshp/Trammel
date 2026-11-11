from django.utils import timezone

from trammel.core import access, panels
from trammel.jobs import hooks as job_hooks
from trammel.workorders import hooks as wo_hooks

from . import services
from .models import TimeEntry


@wo_hooks.check("complete")
def no_running_timers(wo):
    running = TimeEntry.objects.filter(work_order=wo, ended_at__isnull=True)
    for entry in running:
        who = entry.user or entry.machine
        yield f"{who}'s timer is still running."


@wo_hooks.time_logged
def logged_by_operation(wo) -> dict:
    """Minutes logged against each of the work order's operations, by kind."""
    from django.db.models import Sum

    out: dict = {}
    for op_id, kind, minutes in (
        TimeEntry.objects.filter(work_order=wo, operation__isnull=False)
        .values_list("operation_id", "kind")
        .annotate(Sum("minutes"))
        .order_by()
    ):
        out.setdefault(op_id, {})[kind] = minutes or 0
    return out


@job_hooks.register_outstanding
def running_timers(job):
    """§6.2: a forced closeout stops running timers at the current time."""
    for entry in TimeEntry.objects.filter(work_order__job=job, ended_at__isnull=True):
        who = entry.user or entry.machine
        yield job_hooks.Outstanding(
            key=f"timer:{entry.pk}",
            label=f"{who}'s timer on {entry.work_order.number} is running "
            f"(since {timezone.localtime(entry.started_at):%Y-%m-%d %H:%M})",
            choices=[("stop", "Stop it now")],
            resolve=lambda choice, entry=entry: services.stop(entry),
            detail={"timer": str(entry.pk), "started_at": entry.started_at.isoformat()},
        )


@panels.register("workorders.workorder")
def work_order_time(request, wo):
    if not access.has_perm(request.actor, "time.view"):
        return None
    from trammel.shop.models import Machine

    entries = TimeEntry.objects.filter(work_order=wo).select_related("user", "machine")
    return panels.Panel(
        "Time",
        "timetracking/_wo_panel.html",
        {
            "entries": entries[:50],
            "mine": entries.filter(user=request.user, ended_at__isnull=True).first(),
            "machines": Machine.objects.filter(is_active=True),
            "running_machines": entries.filter(kind="machine", ended_at__isnull=True),
            "can_track": access.has_perm(request.actor, "time.track", wo) and wo.is_active,
            "can_costs": access.has_perm(request.actor, "core.see_costs"),
        },
        order=10,
        tab="time",
        count=entries.count(),
    )
