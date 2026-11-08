"""FR-AGR: versioned templates, per-job agreements, acceptance, the work gate."""

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from trammel.agreements import services
from trammel.agreements.models import AgreementSettings, AgreementTemplate, CustomerAgreement
from trammel.core.lifecycle import TransitionError
from trammel.jobs import services as jobs
from trammel.jobs.models import Job


@pytest.fixture
def job(setup_done, customer, admin, monkeypatch):
    from trammel.agreements.wiring import agreement_before_work
    from trammel.jobs import hooks

    # Keep only the agreement gate; quotes have their own tests.
    monkeypatch.setattr(hooks, "_checks", hooks.defaultdict(list))
    hooks.check("in_progress")(agreement_before_work)
    job = jobs.create_job(Job(title="Walnut table", customer=customer))
    for target in ("quoted", "approved"):
        Job.lifecycle.apply(job, target, actor=admin)
    job.refresh_from_db()
    return job


@pytest.mark.django_db
def test_default_template_is_seeded_and_renders(job):
    agreement = services.issue(job, deposit_percent=50)
    assert "Grace Hopper" in agreement.text and job.number in agreement.text
    assert "deposit of 50%" in agreement.text


@pytest.mark.django_db
def test_editing_a_template_makes_a_version_and_keeps_issued_text(job, admin):
    template = AgreementTemplate.default()
    first = services.issue(job)
    services.save_template(template, "New terms for {{ customer.name }}.", user=admin)
    assert template.current.version == 2
    first.refresh_from_db()
    assert "TERMS AND CONDITIONS" in first.text
    second = services.issue(job)
    first.refresh_from_db()
    assert second.text == "New terms for Grace Hopper." and first.status == "superseded"


@pytest.mark.django_db
def test_bad_template_is_refused(admin, setup_done):
    with pytest.raises(ValidationError):
        services.save_template(AgreementTemplate(name="Broken"), "{% if %}", user=admin)


@pytest.mark.django_db
def test_template_sandbox_blocks_unsafe_access(job, admin):
    template = AgreementTemplate.default()
    services.save_template(template, "{{ ''.__class__.__mro__ }}", user=admin)
    from trammel.core.templating import TemplateProblem

    with pytest.raises(TemplateProblem):
        services.issue(job)


@pytest.mark.django_db
def test_work_cannot_start_until_accepted(job, admin):
    with pytest.raises(TransitionError, match="agreement"):
        jobs.transition(job, "in_progress", actor=admin)
    agreement = services.issue(job)
    services.accept(
        agreement, method="manual", accepted_by_name="Grace Hopper", how="phone", user=admin
    )
    jobs.transition(job, "in_progress", actor=admin)


@pytest.mark.django_db
def test_waiver_and_setting(job, admin):
    with pytest.raises(ValidationError):
        services.waive(job, reason=" ")
    services.waive(job, reason="Small counter job")
    assert services.satisfied(job)
    services.unwaive(job)
    assert not services.satisfied(job)
    services.waive(job, reason="Again")  # a waiver can come back after being removed
    services.unwaive(job)
    settings = AgreementSettings.load()
    settings.required = False
    settings.save()
    assert services.satisfied(job)


@pytest.mark.django_db
def test_acceptance_never_carries_over_to_another_job(job, admin, customer):
    services.accept(services.issue(job), method="manual", accepted_by_name="G", user=admin)
    other = jobs.create_job(Job(title="Second job", customer=customer))
    assert services.accepted(other) is None


@pytest.mark.django_db
def test_upload_method_needs_the_document(job, admin):
    agreement = services.issue(job)
    with pytest.raises(ValidationError, match="Upload"):
        services.accept(agreement, method="upload", accepted_by_name="G", user=admin)


@pytest.mark.django_db
def test_screens(admin_client, job):
    response = admin_client.post(f"/agreements/job/{job.pk}/issue/")
    assert response.status_code == 302
    agreement = CustomerAgreement.objects.get()
    page = admin_client.get(f"/jobs/{job.pk}/").content.decode()
    assert "Record acceptance" in page
    response = admin_client.post(
        f"/agreements/{agreement.pk}/accept/",
        {
            "accepted_by_name": "Grace Hopper",
            "accepted_at": "2026-11-07T10:00",
            "method": "upload",
            "how": "in_person",
            "document": SimpleUploadedFile("signed.pdf", b"%PDF-1.4 signed"),
        },
    )
    assert response.status_code == 302
    agreement.refresh_from_db()
    assert agreement.status == "accepted" and agreement.evidence is not None
    for path in (
        "/agreements/templates/",
        "/agreements/templates/new/",
        "/agreements/settings/",
        f"/agreements/{agreement.pk}/",
    ):
        assert admin_client.get(path).status_code == 200, path
