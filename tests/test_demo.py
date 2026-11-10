"""The demo data command builds a whole shop through the real services."""

import pytest
from django.core.management import CommandError, call_command

from trammel.billing.models import Invoice
from trammel.jobs.models import Job
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
    with pytest.raises(CommandError, match="already"):
        call_command("seed_demo", yes=True)


@pytest.mark.django_db
def test_seed_demo_needs_setup_first(db):
    with pytest.raises(CommandError, match="setup"):
        call_command("seed_demo", yes=True)
