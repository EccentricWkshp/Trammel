"""Jobs (FR-JOB-1..9, SPEC §6)."""

import datetime as dt
import json
import re

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from tests.conftest import make_user
from trammel.core.lifecycle import TransitionError
from trammel.core.models import AuditEvent, StateChange
from trammel.customers.models import Customer
from trammel.jobs import hooks, services
from trammel.jobs.models import Job, Status


@pytest.fixture(autouse=True)
def _no_module_gates(no_job_gates):
    """These tests are about the job lifecycle itself; each module tests its own gate."""


@pytest.fixture
def job(setup_done, customer):
    return services.create_job(
        Job(title="Walnut sign", customer=customer, tags=["Laser", "laser "])
    )


def walk(job, *targets, actor):
    for target in targets:
        services.transition(job, target, actor=actor)
        job.refresh_from_db()


@pytest.fixture
def isolated_hooks(monkeypatch):
    """Register hooks for one test only."""
    monkeypatch.setattr(hooks, "_checks", hooks.defaultdict(list))
    monkeypatch.setattr(hooks, "_outstanding", [])
    monkeypatch.setattr(hooks, "_on_duplicate", [])
    return hooks


@pytest.mark.django_db
def test_create_numbers_and_normalizes(job):
    assert job.number.startswith("J-") and job.number.endswith("-0001")
    assert job.status == Status.PROPOSAL
    assert job.tags == ["laser"]
    assert AuditEvent.objects.filter(action="create", entity_id=str(job.pk)).exists()


@pytest.mark.django_db
def test_job_without_customer_uses_the_walk_in_placeholder(setup_done):
    job = services.create_job(Job(title="Counter job"))
    assert job.customer.is_placeholder


@pytest.mark.django_db
def test_full_forward_path(job, admin):
    walk(job, "quoted", "approved", "in_progress", "completed", "closed", actor=admin)
    assert job.status == Status.CLOSED
    assert job.history().count() == 5


@pytest.mark.django_db
def test_cannot_skip_forward(job, admin):
    with pytest.raises(TransitionError):
        services.transition(job, "in_progress", actor=admin)
    with pytest.raises(TransitionError):
        services.transition(job, "closed", actor=admin)  # force only via force_close


@pytest.mark.django_db
def test_hold_requires_reason_and_resumes_to_the_held_state(job, admin):
    walk(job, "quoted", "approved", actor=admin)
    with pytest.raises(TransitionError, match="reason"):
        services.transition(job, "on_hold", actor=admin)
    services.transition(job, "on_hold", actor=admin, reason="Waiting on customer artwork")
    job.refresh_from_db()
    assert (job.status, job.held_from) == ("on_hold", "approved")
    # Only one resume is offered, and it goes back where the job was.
    resumes = [t.target for t in Job.lifecycle.available(job, admin) if t.label == "Resume"]
    assert resumes == ["approved"]
    with pytest.raises(TransitionError):
        services.transition(job, "in_progress", actor=admin)
    services.transition(job, "approved", actor=admin)
    job.refresh_from_db()
    assert (job.status, job.held_from) == ("approved", "")


@pytest.mark.django_db
def test_cancel_needs_a_reason(job, admin):
    with pytest.raises(TransitionError):
        services.transition(job, "cancelled", actor=admin)
    services.transition(job, "cancelled", actor=admin, reason="Customer changed their mind")
    job.refresh_from_db()
    assert job.status == "cancelled" and job.get_status_display() == "Canceled"


@pytest.mark.django_db
def test_reopen_one_step_with_permission(job, admin, setup_done):
    walk(job, "quoted", "approved", "in_progress", "completed", "closed", actor=admin)
    hand = make_user("hand@example.com", "jobs.view", "jobs.edit")
    with pytest.raises(PermissionDenied):
        services.transition(job, "completed", actor=hand, reason="missed a coat")
    services.transition(job, "completed", actor=admin, reason="Missed a coat of finish")
    services.transition(job, "in_progress", actor=admin, reason="Refinishing")
    job.refresh_from_db()
    assert job.status == "in_progress"
    assert StateChange.objects.filter(entity_id=job.pk, kind="reopen").count() == 2


@pytest.mark.django_db
def test_registered_checks_block_transitions(job, admin, isolated_hooks):
    @isolated_hooks.check("in_progress")
    def agreement(j):
        yield "The customer agreement has not been accepted."

    walk(job, "quoted", "approved", actor=admin)
    with pytest.raises(TransitionError) as exc:
        services.transition(job, "in_progress", actor=admin)
    assert exc.value.problems == ["The customer agreement has not been accepted."]


@pytest.mark.django_db
def test_force_close_resolves_outstanding_and_marks_job(job, admin, isolated_hooks):
    resolved = {}

    @isolated_hooks.register_outstanding
    def unpaid(j):
        yield hooks.Outstanding(
            key="invoice:1",
            label="INV-1 balance $120.00",
            choices=[("leave", "Leave outstanding"), ("write_off", "Write off")],
            resolve=lambda choice: resolved.update(invoice=choice),
            detail={"balance": "120.00"},
        )

    @isolated_hooks.check("closed")
    def unpaid_check(j):
        yield "INV-1 is unpaid."

    walk(job, "quoted", "approved", "in_progress", actor=admin)
    with pytest.raises(TransitionError, match="reason"):
        services.force_close(job, actor=admin, reason="", resolutions={})
    assert resolved == {}  # nothing resolved by a refused closeout

    services.force_close(
        job, actor=admin, reason="Customer unreachable", resolutions={"invoice:1": "write_off"}
    )
    job.refresh_from_db()
    assert job.status == "closed" and job.force_closed_at is not None
    assert resolved == {"invoice": "write_off"}
    change = StateChange.objects.get(entity_id=job.pk, kind="force")
    assert change.details["outstanding"][0]["resolution"] == "write_off"
    assert change.details["checks_failing"] == ["INV-1 is unpaid."]


@pytest.mark.django_db
def test_force_close_needs_permission(job, setup_done):
    manager = make_user("mgr@example.com", "jobs.view", "jobs.edit")
    with pytest.raises(PermissionDenied):
        services.force_close(job, actor=manager, reason="mess", resolutions={})


@pytest.mark.django_db
def test_invalid_resolution_is_refused(job, admin, isolated_hooks):
    @isolated_hooks.register_outstanding
    def timer(j):
        yield hooks.Outstanding(key="timer:1", label="Timer", choices=[("stop", "Stop now")])

    with pytest.raises(ValidationError):
        services.force_close(job, actor=admin, reason="x", resolutions={"timer:1": "explode"})


@pytest.mark.django_db
def test_closed_jobs_cannot_be_edited(job, admin):
    walk(job, "quoted", "approved", "in_progress", "completed", "closed", actor=admin)
    from trammel.core import audit

    before = audit.snapshot(job)
    job.title = "Changed"
    with pytest.raises(ValidationError):
        services.update_job(job, before=before)


@pytest.mark.django_db
def test_replacing_the_placeholder_customer_is_recorded(setup_done, customer):
    from trammel.core import audit

    job = services.create_job(Job(title="Walk-in"))
    before = audit.snapshot(job)
    job.customer = customer
    services.update_job(job, before=before)
    assert AuditEvent.objects.filter(action="job.customer_replaced").exists()


@pytest.mark.django_db
def test_duplicate_copies_scope_not_dates(job, admin, isolated_hooks):
    calls = []
    isolated_hooks.register_duplicate(lambda src, new: calls.append((src.pk, new.pk)))
    job.due_date = dt.date(2026, 12, 1)
    job.save()
    copy = services.duplicate_job(job)
    assert copy.number != job.number and copy.title == job.title and copy.due_date is None
    assert copy.status == "proposal" and calls == [(job.pk, copy.pk)]


@pytest.mark.django_db
def test_only_early_or_dead_jobs_can_be_deleted(job, admin):
    services.delete_job(job)
    assert Job.all_objects.get(pk=job.pk).is_deleted
    other = services.create_job(Job(title="Started", customer=job.customer))
    walk(other, "quoted", "approved", "in_progress", actor=admin)
    with pytest.raises(ValidationError):
        services.delete_job(other)


@pytest.mark.django_db
def test_schedule_flags(setup_done, customer):
    today = dt.date(2026, 11, 10)
    job = Job(
        title="x",
        customer=customer,
        status="in_progress",
        promised_date=dt.date(2026, 11, 9),
        due_date=dt.date(2026, 11, 12),
    )
    assert [level for level, _ in job.schedule_flags(today)] == ["late", "risk"]
    job.status = "completed"
    assert job.schedule_flags(today) == []


# ---------------------------------------------------------------------------
# Screens and API
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_job_screens(admin_client, job):
    for path in (
        "/jobs/",
        "/jobs/?status=all&tag=laser",
        "/jobs/calendar/",
        "/jobs/calendar/?month=2026-02",
        "/jobs/calendar/?month=bogus",
        f"/jobs/{job.pk}/",
        f"/jobs/{job.pk}/edit/",
        f"/jobs/{job.pk}/force-close/",
        "/jobs/new/",
        f"/customers/{job.customer_id}/",
    ):
        assert admin_client.get(path).status_code == 200, path
    assert (
        job.number in admin_client.get(f"/customers/{job.customer_id}/?tab=jobs").content.decode()
    )


@pytest.mark.django_db
def test_transition_screen_reports_problems(admin_client, job):
    response = admin_client.post(f"/jobs/{job.pk}/transition/", {"target": "closed"}, follow=True)
    assert "Cannot move" in response.content.decode()
    admin_client.post(f"/jobs/{job.pk}/transition/", {"target": "quoted"})
    job.refresh_from_db()
    assert job.status == "quoted"


@pytest.mark.django_db
def test_force_close_screen_needs_confirmation(admin_client, job):
    url = f"/jobs/{job.pk}/force-close/"
    response = admin_client.post(url, {"reason": "Bad data"})
    assert "confirmation" in response.content.decode()
    admin_client.post(url, {"reason": "Bad data", "confirm": "yes"})
    job.refresh_from_db()
    assert job.status == "closed" and job.force_closed_at


@pytest.mark.django_db
def test_calendar_places_jobs_on_their_dates(admin_client, job):
    job.promised_date = dt.date(2026, 2, 14)
    job.save()
    page = admin_client.get("/jobs/calendar/?month=2026-02").content.decode()
    assert "Promised" in page and job.number in page


@pytest.mark.django_db
def test_job_form_offers_tags_already_in_use(admin_client, customer):
    services.create_job(Job(title="A", customer=customer, tags=["laser", "signage"]))
    services.create_job(Job(title="B", customer=customer, tags=["laser"]))
    page = admin_client.get("/jobs/new/").content.decode()
    # Offered as choices, the most used first.
    assert page.index('value="laser"') < page.index('value="signage"')
    response = admin_client.post(
        "/jobs/new/",
        {
            "title": "C",
            "customer": str(customer.pk),
            "tags": ["signage"],
            "new_tags": "Walnut, laser",
        },
    )
    assert response.status_code == 302
    job = Job.objects.get(title="C")
    assert job.tags == ["signage", "walnut", "laser"]
    edit = admin_client.get(f"/jobs/{job.pk}/edit/").content.decode()
    assert re.search(r'<input[^>]*value="walnut"[^>]*checked', edit)
    assert not re.search(r'<input[^>]*value="signage2"', edit)


@pytest.mark.django_db
def test_job_form_without_existing_tags_takes_free_text(admin_client, customer):
    page = admin_client.get("/jobs/new/").content.decode()
    assert 'name="tags"' not in page and 'name="new_tags"' in page
    admin_client.post("/jobs/new/", {"title": "D", "customer": str(customer.pk), "new_tags": "cnc"})
    assert Job.objects.get(title="D").tags == ["cnc"]


@pytest.mark.django_db
def test_calendar_week_starts_on_sunday_by_default(admin_client, setup_done):
    from trammel.core.models import BusinessProfile

    response = admin_client.get("/jobs/calendar/?month=2026-11")
    assert response.context["weekdays"][0] == "Sun"
    # November 2026 begins on a Sunday, so the first cell is the 1st.
    assert response.context["weeks"][0][0][0] == dt.date(2026, 11, 1)
    business = BusinessProfile.load()
    business.week_start = 0
    business.save()
    response = admin_client.get("/jobs/calendar/?month=2026-11")
    assert response.context["weekdays"][0] == "Mon"
    assert response.context["weeks"][0][0][0] == dt.date(2026, 10, 26)


@pytest.mark.django_db
def test_customer_sees_only_their_jobs_through_scoping(setup_done, customer):
    from trammel.core import access

    other = Customer.objects.create(name="Other")
    mine = services.create_job(Job(title="Mine", customer=customer))
    services.create_job(Job(title="Theirs", customer=other))
    actor = access.Actor(kind="customer", customer_id=customer.pk)
    assert list(access.scoped(actor, "jobs.view", Job.objects.all())) == [mine]


@pytest.mark.django_db
def test_jobs_api(admin_client, customer):
    created = admin_client.post(
        "/api/v1/jobs/",
        json.dumps({"title": "API job", "customer": str(customer.pk), "due_date": "2026-12-01"}),
        content_type="application/json",
    )
    assert created.status_code == 201
    job_id = created.json()["id"]
    moved = admin_client.post(
        f"/api/v1/jobs/{job_id}/transition",
        json.dumps({"target": "quoted"}),
        content_type="application/json",
    )
    assert moved.json()["status"] == "quoted"
    skipped = admin_client.post(
        f"/api/v1/jobs/{job_id}/transition",
        json.dumps({"target": "completed"}),
        content_type="application/json",
    )
    assert skipped.status_code == 409
    assert admin_client.get(f"/api/v1/jobs/{job_id}/outstanding").json() == []
    closed = admin_client.post(
        f"/api/v1/jobs/{job_id}/force-close",
        json.dumps({"reason": "test"}),
        content_type="application/json",
    )
    assert closed.json()["force_closed_at"] is not None
