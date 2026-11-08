from trammel.core import access, panels
from trammel.jobs import hooks

from . import services
from .models import AgreementSettings, AgreementWaiver


@hooks.check("in_progress")
def agreement_before_work(job):
    if not services.satisfied(job):
        yield "The customer agreement has not been accepted (or waived for this job)."


@panels.register("jobs.job")
def job_agreement(request, job):
    if not access.has_perm(request.actor, "agreements.view"):
        return None
    return panels.Panel(
        "Agreement",
        "agreements/_job_panel.html",
        {
            "agreements": job.agreements.filter(is_primary=True),
            "accepted": services.accepted(job),
            "waiver": AgreementWaiver.objects.filter(job=job).first(),
            "required": AgreementSettings.load().required,
            "can_manage": access.has_perm(request.actor, "agreements.manage", job),
        },
        order=5,
    )
