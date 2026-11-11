"""Issuing and accepting customer agreements (FR-AGR-3, -4, -7)."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from trammel.core import audit, templating
from trammel.core.models import BusinessProfile

from .models import (
    AgreementSettings,
    AgreementTemplate,
    AgreementTemplateVersion,
    AgreementWaiver,
    CustomerAgreement,
)


@transaction.atomic
def save_template(template: AgreementTemplate, body: str, *, user=None) -> AgreementTemplateVersion:
    """Save a template; a changed body becomes a new version (FR-AGR-2)."""
    problems = templating.validate(body, html=False)
    if problems:
        raise ValidationError(problems)
    creating = template._state.adding
    template.full_clean()
    template.save()
    if template.is_default:
        AgreementTemplate.objects.exclude(pk=template.pk).update(is_default=False)
    current = template.current
    if current is not None and current.body == body:
        return current
    version = AgreementTemplateVersion.objects.create(
        template=template,
        version=(current.version + 1) if current else 1,
        body=body,
        created_by=user,
    )
    audit.record(
        "agreement_template.version",
        template,
        summary=f"{'Created' if creating else 'Edited'} agreement template {template} "
        f"(version {version.version})",
    )
    return version


def context_for(job, **extra) -> dict:
    business = BusinessProfile.load()
    return {
        "business": templating.business_context(business),
        "customer": templating.customer_context(job.customer),
        "job": {
            "number": job.number,
            "title": job.title,
            "description": job.description,
            "due_date": job.due_date,
            "promised_date": job.promised_date,
        },
        "payment_terms_days": job.customer.effective_payment_terms(business),
        "today": timezone.localdate(),
        **extra,
    }


@transaction.atomic
def issue(
    job, *, template: AgreementTemplate | None = None, primary: bool = True, **context
) -> CustomerAgreement:
    """Issue an agreement for this job from a template's current version.

    An unaccepted earlier agreement for the job is superseded. An acceptance on
    another job never carries over (FR-AGR-3): agreements belong to one job.
    """
    template = template or AgreementTemplate.default()
    if template is None or template.current is None:
        raise ValidationError("There is no agreement template. Create one in Settings first.")
    version = template.current
    text = templating.render(version.body, context_for(job, **context), html=False)
    if primary:
        job.agreements.filter(is_primary=True, status=CustomerAgreement.Status.ISSUED).update(
            status=CustomerAgreement.Status.SUPERSEDED
        )
    agreement = CustomerAgreement.objects.create(
        job=job,
        template_version=version,
        text=text,
        is_primary=primary,
        created_by=audit.current_actor().user,
    )
    audit.record("agreement.issue", agreement, summary=f"Issued {version} for {job.number}")
    return agreement


def preview_text(job, **context) -> str:
    """The text `issue` would give the job's agreement now, without issuing it:
    what a customer reads in the portal before accepting it."""
    template = AgreementTemplate.default()
    if template is None or template.current is None:
        raise ValidationError("There is no agreement template. Create one in Settings first.")
    return templating.render(template.current.body, context_for(job, **context), html=False)


@transaction.atomic
def accept(
    agreement: CustomerAgreement,
    *,
    method: str,
    accepted_by_name: str,
    accepted_at=None,
    how: str = "",
    notes: str = "",
    evidence=None,
    ip=None,
    user=None,
) -> CustomerAgreement:
    """Record acceptance with its evidence (FR-AGR-7)."""
    settings = AgreementSettings.load()
    if agreement.status != CustomerAgreement.Status.ISSUED:
        raise ValidationError(f"This agreement is {agreement.get_status_display().lower()}.")
    if method == CustomerAgreement.Method.MANUAL and not settings.manual_enabled:
        raise ValidationError("Staff-recorded acceptance is turned off.")
    if method == CustomerAgreement.Method.CLICK and not settings.click_enabled:
        raise ValidationError(
            "Accepting the agreement in the portal is turned off. Please contact us to approve."
        )
    if method == CustomerAgreement.Method.UPLOAD:
        if not settings.upload_enabled:
            raise ValidationError("Acceptance by uploaded document is turned off.")
        if evidence is None:
            raise ValidationError("Upload the signed document.")
    if not accepted_by_name.strip():
        raise ValidationError("Who accepted the agreement?")
    agreement.status = CustomerAgreement.Status.ACCEPTED
    agreement.method = method
    agreement.how = how
    agreement.accepted_by_name = accepted_by_name.strip()
    agreement.accepted_at = accepted_at or timezone.now()
    agreement.notes = notes
    agreement.evidence = evidence
    agreement.evidence_ip = ip
    agreement.recorded_by = user
    agreement.save()
    audit.record(
        "agreement.accept",
        agreement,
        summary=f"{agreement.accepted_by_name} accepted the agreement for {agreement.job.number} "
        f"({agreement.get_method_display().lower()})",
        after={"method": method, "how": how, "accepted_at": agreement.accepted_at},
    )
    return agreement


def accepted(job) -> CustomerAgreement | None:
    return job.agreements.filter(is_primary=True, status=CustomerAgreement.Status.ACCEPTED).first()


def satisfied(job) -> bool:
    """Work may start: accepted, waived, or not required (FR-AGR-4)."""
    if job.customer.is_internal or not AgreementSettings.load().required:
        return True
    return accepted(job) is not None or AgreementWaiver.objects.filter(job=job).exists()


@transaction.atomic
def waive(job, *, reason: str) -> AgreementWaiver:
    if not reason.strip():
        raise ValidationError("A reason is required to waive the agreement.")
    waiver, _ = AgreementWaiver.all_objects.update_or_create(
        job=job, defaults={"reason": reason.strip(), "deleted_at": None}
    )
    audit.record(
        "agreement.waive",
        job,
        summary=f"Waived the agreement for {job.number}",
        reason=reason.strip(),
    )
    return waiver


@transaction.atomic
def unwaive(job) -> None:
    AgreementWaiver.objects.filter(job=job).delete()
    audit.record(
        "agreement.unwaive", job, summary=f"Restored the agreement requirement for {job.number}"
    )
