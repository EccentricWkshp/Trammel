"""The demo data command builds a whole shop through the real services."""

import pytest
from django.core.management import CommandError, call_command

from trammel.billing.models import Invoice
from trammel.customers.models import Customer
from trammel.jobs.models import Job
from trammel.purchasing.models import PurchaseOrder
from trammel.stock import services as stock
from trammel.workorders.models import MaterialUsage, WorkOrder


@pytest.mark.django_db
def test_seed_demo_builds_every_stage_once(admin):
    call_command("seed_demo", yes=True)
    statuses = set(Job.objects.values_list("status", flat=True))
    assert {
        "proposal",
        "quoted",
        "approved",
        "in_progress",
        "completed",
        "closed",
        "on_hold",
    } <= statuses
    assert WorkOrder.objects.filter(status="waiting").exists()
    assert Invoice.objects.filter(kind="deposit", status="paid").count() == 2
    assert any(i.is_overdue for i in Invoice.objects.all())
    assert Invoice.objects.filter(kind="direct_sale", status="paid").exists()
    # Tracked materials used on work orders came out of stock, and the ledger adds up.
    assert MaterialUsage.objects.filter(source="stock").exists()
    assert stock.restock_list().exists() and stock.check_ledger() == []
    states = sorted(PurchaseOrder.objects.values_list("status", flat=True))
    assert states == ["closed", "draft", "ordered"]
    with pytest.raises(CommandError, match="already"):
        call_command("seed_demo", yes=True)
    # Paid online (recorded only), the portal, and files waiting for review.
    from trammel.billing.models import Payment
    from trammel.payments.models import OnlinePayment
    from trammel.portal.models import Access, Upload

    assert set(Payment.objects.values_list("method", flat=True)) >= {"stripe", "paypal"}
    assert OnlinePayment.objects.filter(status="recorded").count() == 2
    assert Access.objects.filter(enabled=True).count() == 4
    assert Upload.objects.filter(status="pending").count() == 2
    assert Upload.objects.filter(status="accepted").count() == 1
    count = Upload.objects.count()
    call_command("seed_demo", yes=True, only="portal")
    assert Upload.objects.count() == count  # added once
    # The schedule part: moves, pins and a printer group, added once.
    from trammel.workorders import scheduling

    plan = scheduling.build()
    titles = [p.wo.title for p in plan.work_orders]
    assert "Reorganize the lumber rack" in titles
    assert any(p.moved for p in plan.work_orders)
    ops = [o for p in plan.work_orders for o in p.ops]
    assert any(o.pinned and any(b.after_hours for b in o.bookings) for o in ops)
    assert any(o.too_early for o in ops)
    assert any(o.other_machine for o in ops)
    # Added once, even after its work orders are renamed; its printers run unattended.
    from trammel.shop.models import Machine

    WorkOrder.objects.filter(title="Rush: 40 engraved tap handles").update(title="Tap handles")
    count = WorkOrder.objects.count()
    call_command("seed_demo", yes=True, only="schedule")
    assert WorkOrder.objects.count() == count
    assert Machine.objects.get(name="Prusa MK4").runs_unattended


@pytest.mark.django_db
def test_the_schedule_part_adds_alone_without_notifying(admin, monkeypatch):
    from trammel.notifications import wiring
    from trammel.quotes import services as quotes

    sent = []
    monkeypatch.setattr(wiring, "notify", lambda kind, **kw: sent.append(kind))
    hooks = list(quotes.on_approved)
    Customer.objects.create(name="Tom Becker", email="tom@demo.trammel.test")
    call_command("seed_demo", yes=True, only="schedule")
    assert WorkOrder.objects.filter(title="Rush: 40 engraved tap handles").exists()
    tom = Job.objects.get(title="Walnut keepsake box")
    assert tom.customer.name == "Tom Becker" and tom.status == "approved"
    # Quotes approved without notifying staff, and the hook back afterward.
    assert sent == [] and quotes.on_approved == hooks


@pytest.mark.django_db
def test_the_models_part_adds_a_tap_handle_and_a_bracket_once(
    admin, monkeypatch, django_capture_on_commit_callbacks
):
    from decimal import Decimal

    from trammel.core import tasks
    from trammel.files.services import attachments_for
    from trammel.notifications import wiring
    from trammel.shop.models import Material

    pytest.importorskip("OCP", reason="the bracket is a STEP file, made with OpenCascade")
    sent = []
    monkeypatch.setattr(wiring, "notify", lambda kind, **kw: sent.append(kind))
    Customer.objects.create(name="Ava Lindqvist", email="ava@demo.trammel.test")
    with django_capture_on_commit_callbacks(execute=True):
        call_command("seed_demo", yes=True, only="schedule")
        call_command("seed_demo", yes=True, only="models")
    tasks.run_pending()

    steel = Material.objects.get(name="Mild steel sheet, HRPO")
    assert steel.k_factor == Decimal("0.36") and steel.thickness == "0.187 in"
    job = Job.objects.get(tags__contains=["models"])
    assert job.customer.name == "Ava Lindqvist" and job.status == "approved"
    wo = WorkOrder.objects.get(job=job)
    (bracket,) = [link.document for link in attachments_for(job)]
    blob = bracket.current.blob
    # Previewed, measured in the inches it was drawn in, and unfolded.
    assert blob.model["unit"] == "inch" and blob.model["sheet"]["bends"] == 1
    assert blob.flat
    rush = WorkOrder.objects.get(title="Rush: 40 engraved tap handles").job
    (handle,) = [link.document for link in attachments_for(rush)]
    assert handle.current.blob.model["solid"] is True
    assert sent == []
    count = Job.objects.count()
    call_command("seed_demo", yes=True, only="models")
    assert Job.objects.count() == count  # added once
    assert WorkOrder.objects.filter(job=job).count() == 1 and wo.job_id == job.pk


@pytest.mark.django_db
def test_seed_demo_needs_setup_first(db):
    with pytest.raises(CommandError, match="setup"):
        call_command("seed_demo", yes=True)
