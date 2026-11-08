from trammel.core import access, panels

from .models import Job


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
