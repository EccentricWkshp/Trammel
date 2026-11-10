"""FR-WO, FR-TIME and the shop-floor app."""

import datetime as dt
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from tests.conftest import make_user
from trammel.agreements.models import AgreementSettings
from trammel.core.lifecycle import TransitionError
from trammel.core.models import AuditEvent, ShopRates
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.shop.models import Machine, Material
from trammel.timetracking import services as time
from trammel.timetracking.models import TimeEntry
from trammel.workorders import services

D = Decimal


@pytest.fixture
def approved(setup_done, customer, admin):
    """A job with an approved quote and accepted agreement — ready for work."""
    rates = ShopRates.load()
    rates.labor_hourly_rate = D("60")
    rates.save()
    job = jobs.create_job(Job(title="Bookshelf", customer=customer))
    rev = quotes.create_quote(job)
    laser = Machine.objects.create(name="Laser", kind="laser", hourly_rate=D("90"))
    ply = Material.objects.create(name="Ply", category="sheet", unit="sheet", unit_cost=D("40"))
    form = LINE_FORMS["machine"]({"machine": str(laser.pk), "quantity": "1"})
    assert form.is_valid()
    quotes.save_line(form.build(rev))
    form = LINE_FORMS["material"]({"material": str(ply.pk), "quantity": "2"})
    assert form.is_valid()
    quotes.save_line(form.build(rev))
    quotes.send(rev, actor=admin)
    quotes.approve(rev, actor=admin, approved_by_name="Grace", accept_agreement=True, user=admin)
    job.refresh_from_db()
    rev.refresh_from_db()
    return job, rev, laser, ply


@pytest.fixture
def wo(approved):
    job, rev, *_ = approved
    return services.create(job, revision=rev)


@pytest.mark.django_db
def test_work_order_carries_over_the_quote(wo, approved):
    _job, _rev, laser, ply = approved
    assert wo.number.startswith("WO-")
    ops = list(wo.operations.all())
    assert [op.machine for op in ops] == [laser, None]
    assert wo.planned_materials.get().material == ply


@pytest.mark.django_db
def test_no_work_order_before_approval_or_agreement(setup_done, customer, admin):
    job = jobs.create_job(Job(title="x", customer=customer))
    with pytest.raises(ValidationError, match="approved"):
        services.create(job)


@pytest.mark.django_db
def test_starting_a_timer_starts_the_work_order_and_the_job(wo, admin):
    entry = time.start(wo, user=admin)
    wo.refresh_from_db()
    assert wo.status == "in_progress" and wo.job.status == "in_progress"
    with pytest.raises(ValidationError, match="already have"):
        time.start(wo, user=admin)
    entry.started_at = timezone.now() - dt.timedelta(minutes=90)
    entry.save()
    time.stop(entry)
    entry.refresh_from_db()
    assert entry.minutes == D("90.00") and entry.cost == D("90.00")  # 1.5 h at 60


@pytest.mark.django_db
def test_machine_timers_are_separate(wo, admin, approved):
    _job, _rev, laser, _ply = approved
    time.start(wo, user=admin)
    time.start(wo, kind="machine", machine=laser)
    with pytest.raises(ValidationError, match="Laser already has"):
        time.start(wo, kind="machine", machine=laser)


@pytest.mark.django_db
def test_user_cost_rate_is_used(wo, setup_done):
    hand = make_user("hand@example.com", "time.track", "workorders.view")
    hand.labor_cost_rate = D("25")
    hand.save()
    entry = time.manual(wo, kind="labor", minutes=D("120"), started_at=timezone.now(), user=hand)
    assert entry.cost == D("50.00") and entry.bill_rate == D("60")
    assert AuditEvent.objects.filter(action="create", entity_id=str(entry.pk)).exists()


@pytest.mark.django_db
def test_closeout_checks(wo, admin):
    services.transition(wo, "in_progress", actor=admin)
    entry = time.start(wo, user=admin)
    with pytest.raises(TransitionError) as exc:
        services.transition(wo, "complete", actor=admin)
    assert any("not done" in p for p in exc.value.problems)
    assert any("timer is still running" in p for p in exc.value.problems)
    for op in wo.operations.all():
        services.set_operation(op, "done", user=admin)
    time.stop(entry)
    services.transition(wo, "complete", actor=admin)
    with pytest.raises(TransitionError, match="material use"):
        services.transition(wo, "closed", actor=admin)
    services.confirm_materials(wo)
    services.transition(wo, "closed", actor=admin)


@pytest.mark.django_db
def test_waiting_needs_a_reason_and_what(wo, admin):
    services.transition(wo, "in_progress", actor=admin)
    with pytest.raises(ValidationError):
        services.transition(wo, "waiting", actor=admin, reason="parts")
    services.transition(
        wo, "waiting", actor=admin, reason="Hinges on order", waiting_on="materials"
    )
    wo.refresh_from_db()
    assert wo.status == "waiting" and wo.waiting_on == "materials"


@pytest.mark.django_db
def test_job_cannot_complete_with_open_work_orders(wo, admin):
    services.transition(wo, "in_progress", actor=admin)
    with pytest.raises(TransitionError, match=wo.number):
        jobs.transition(wo.job, "completed", actor=admin)


@pytest.mark.django_db
def test_reopening_a_work_order_reopens_the_job(wo, admin):
    services.transition(wo, "in_progress", actor=admin)
    for op in wo.operations.all():
        services.set_operation(op, "done", user=admin)
    services.transition(wo, "complete", actor=admin)
    services.confirm_materials(wo)
    services.transition(wo, "closed", actor=admin)
    job = wo.job
    jobs.transition(job, "completed", actor=admin)
    jobs.transition(job, "closed", actor=admin)
    services.transition(wo, "complete", actor=admin, reason="Missed a coat")
    job.refresh_from_db()
    assert job.status == "in_progress"


@pytest.mark.django_db
def test_force_close_cancels_work_orders_and_stops_timers(wo, admin):
    entry = time.start(wo, user=admin)
    job = wo.job
    jobs.force_close(job, actor=admin, reason="Customer gone", resolutions={})
    wo.refresh_from_db()
    entry.refresh_from_db()
    job.refresh_from_db()
    assert job.status == "closed" and wo.status == "cancelled" and not entry.is_running


@pytest.mark.django_db
def test_material_use_and_notes_are_append_only(wo, admin, approved):
    _job, _rev, _laser, ply = approved
    [usage] = services.record_material(wo, material=ply, quantity=D("1.5"), unit="", user=admin)
    assert usage.total_cost == D("60.00")
    usage.quantity = D("2")
    with pytest.raises(ValidationError):
        usage.save()
    services.record_material(wo, material=ply, quantity=D("-0.5"), unit="", user=admin)
    note = services.add_note(wo, text="Glued up", user=admin)
    with pytest.raises(ValidationError):
        note.delete()


@pytest.mark.django_db
def test_screens_and_traveler(admin_client, wo):
    for path in (
        f"/work-orders/{wo.pk}/",
        "/work-orders/",
        f"/work-orders/{wo.pk}/edit/",
        f"/time/add/{wo.pk}/",
        "/time/",
        f"/jobs/{wo.job_id}/",
    ):
        assert admin_client.get(path).status_code == 200, path
    response = admin_client.get(f"/work-orders/{wo.pk}/traveler/")
    traveler = response.content.decode()
    assert wo.number in traveler and "<svg" in traveler and "$" not in traveler
    # Its inline styles must be allowed, or the page prints unstyled.
    assert "style-src 'unsafe-inline'" in response["Content-Security-Policy"]
    assert "Prices are never shown" not in traveler and "<script" not in traveler
    printed = admin_client.get(f"/work-orders/{wo.pk}/traveler/?print=1").content.decode()
    assert "print();" in printed
    admin_client.post(f"/time/start/{wo.pk}/", {"kind": "labor"})
    assert TimeEntry.objects.filter(work_order=wo, ended_at__isnull=True).count() == 1
    admin_client.post(
        f"/work-orders/{wo.pk}/notes/",
        {"text": "Photo of glue-up", "photo": SimpleUploadedFile("p.txt", b"x")},
    )
    assert wo.notes.count() == 1


@pytest.mark.django_db
def test_floor_pages(admin_client, wo):
    assert admin_client.get("/floor/").status_code == 200
    assert admin_client.get("/floor/scan/").status_code == 200
    page = admin_client.get(f"/floor/wo/{wo.pk}/").content.decode()
    assert "Start my timer" in page and wo.number in page
    found = admin_client.get(f"/floor/find/?q={wo.number.lower()}")
    assert found.url == f"/floor/wo/{wo.pk}/"
    scanned = admin_client.get(f"/floor/find/?q=https://shop.example/floor/wo/{wo.pk}/")
    assert scanned.url == f"/floor/wo/{wo.pk}/"
    manifest = admin_client.get("/floor/manifest.webmanifest")
    assert manifest["Content-Type"] == "application/manifest+json"
    assert admin_client.get("/floor/sw.js")["Service-Worker-Allowed"] == "/floor/"


@pytest.mark.django_db
def test_agreement_gate_applies_to_work_orders(setup_done, customer, admin, no_job_gates):
    settings = AgreementSettings.load()
    assert settings.required
    job = jobs.create_job(Job(title="x", customer=customer))
    for target in ("quoted", "approved"):
        Job.lifecycle.apply(job, target, actor=admin)
    with pytest.raises(ValidationError, match="agreement"):
        services.create(job)


@pytest.mark.django_db
def test_operations_can_be_edited_reordered_and_deleted(admin_client, wo, admin, approved):
    _job, _rev, laser, _ply = approved
    first, second = list(wo.operations.all())[:2]
    base = f"/work-orders/{wo.pk}/operations"

    admin_client.post(
        f"{base}/{first.pk}/edit/",
        {"name": "Cut parts", "description": "Nest tight", "machine": str(laser.pk)},
    )
    first.refresh_from_db()
    assert (first.name, first.description, first.machine) == ("Cut parts", "Nest tight", laser)
    assert AuditEvent.objects.filter(action="operation.edit").exists()
    response = admin_client.post(f"{base}/{first.pk}/edit/", {"name": " "}, follow=True)
    assert "Name the operation" in response.content.decode()

    admin_client.post(f"{base}/{second.pk}/move/", {"direction": "up"})
    assert list(wo.operations.values_list("pk", flat=True))[:2] == [second.pk, first.pk]
    admin_client.post(f"{base}/{second.pk}/move/", {"direction": "up"})  # already first
    assert wo.operations.first().pk == second.pk

    entry = time.start(wo, user=admin)
    entry.operation = first
    entry.save()
    time.stop(entry)
    response = admin_client.post(f"{base}/{first.pk}/delete/", follow=True)
    assert "stay on the work order" in response.content.decode()
    assert not wo.operations.filter(pk=first.pk).exists()
    entry.refresh_from_db()
    assert entry.operation is None and entry.work_order == wo


@pytest.mark.django_db
def test_operations_are_locked_on_a_closed_work_order(wo, admin):
    op = wo.operations.first()
    type(wo).objects.filter(pk=wo.pk).update(status="closed")
    op.refresh_from_db()
    with pytest.raises(ValidationError, match="Reopen"):
        services.update_operation(op, name="New")
    with pytest.raises(ValidationError, match="Reopen"):
        services.delete_operation(op)
