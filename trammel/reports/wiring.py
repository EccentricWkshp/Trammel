from trammel.core import access, panels

from . import services


@panels.register("jobs.job")
def job_costing(request, job):
    """Live quoted-versus-actual roll-up (FR-JOB-3). Margins need the costs permission."""
    if not access.has_perm(request.actor, "jobs.view"):
        return None
    costing = services.job_costing(job)
    if not (costing.quoted_total or costing.actual_total or costing.invoiced):
        return None
    return panels.Panel(
        "Job costing",
        "reports/_job_costing.html",
        {
            "c": costing,
            "can_costs": access.has_perm(request.actor, "core.see_costs"),
        },
        order=40,
    )
