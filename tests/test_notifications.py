"""FR-NTF-1..3: email through the queue, toggles, templates, the outbound switch."""

import datetime as dt
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone

from trammel.billing import services as billing
from trammel.core import secrets, tasks
from trammel.core.models import Credential, SystemSettings
from trammel.notifications import services
from trammel.notifications.models import EmailLog, SmtpSettings


@pytest.fixture
def smtp(setup_done):
    s = SmtpSettings.load()
    s.host, s.from_address, s.from_name = "smtp.example.com", "shop@example.com", "Test Shop"
    s.save()
    return s


@pytest.mark.django_db
def test_notify_queues_then_sends(smtp, admin, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        entry = services.notify("backup_failed", context={"when": "today", "error": "disk full"})
    assert entry.status == "queued" and entry.to == "admin@example.com"
    tasks.run_pending()
    assert len(mail.outbox) == 1 and "disk full" in mail.outbox[0].body
    assert mail.outbox[0].from_email == "Test Shop <shop@example.com>"
    entry.refresh_from_db()
    assert entry.status == "sent"


@pytest.mark.django_db
def test_offline_and_disabled_are_skipped(smtp, admin):
    tpl = services.template_for("backup_failed")
    tpl.enabled = False
    tpl.save()
    assert services.notify("backup_failed", context={}).status == "skipped"
    tpl.enabled = True
    tpl.save()
    system = SystemSettings.load()
    system.offline_mode = True
    system.save()
    entry = services.notify("backup_failed", context={})
    assert entry.status == "skipped" and "Outbound" in entry.detail


@pytest.mark.django_db
def test_templates_are_editable_and_sandboxed(smtp, admin):
    tpl = services.template_for("backup_failed")
    tpl.subject = "{{ ''.__class__.__mro__ }}"
    tpl.save()
    assert services.notify("backup_failed", context={}).status == "failed"


@pytest.mark.django_db
def test_payment_receipt(smtp, customer, admin, django_capture_on_commit_callbacks):
    inv = billing.create(customer, kind="direct_sale")
    billing.add_line(inv, description="x", quantity=1, unit_price=Decimal("10"))
    customer.tax_exempt, customer.tax_exempt_number = True, "X"
    customer.save()
    inv = billing.issue(inv, actor=admin)
    with django_capture_on_commit_callbacks(execute=True):
        billing.record_payment(
            customer,
            amount=Decimal("15"),
            received_on=timezone.localdate(),
            method="cash",
            allocations={inv: Decimal("10")},
        )
    tasks.run_pending()
    receipt = mail.outbox[-1]
    assert receipt.to == ["grace@example.com"] and "$15.00" in receipt.body
    assert "$5.00 is held as credit" in receipt.body


@pytest.mark.django_db
def test_overdue_reminder_once_per_interval(
    smtp, customer, admin, django_capture_on_commit_callbacks
):
    customer.tax_exempt, customer.tax_exempt_number = True, "X"
    customer.save()
    inv = billing.create(customer, kind="direct_sale", invoice_date=dt.date(2026, 1, 1))
    billing.add_line(inv, description="x", quantity=1, unit_price=Decimal("10"))
    billing.issue(inv, actor=admin)
    from trammel.notifications.tasks import overdue_reminders

    with django_capture_on_commit_callbacks(execute=True):
        overdue_reminders({})
        overdue_reminders({})
    assert EmailLog.objects.filter(event="invoice_overdue").count() == 1


@pytest.mark.django_db
def test_email_quote_and_settings_screen(admin_client, smtp, admin):
    response = admin_client.post(
        "/settings/email/",
        {
            "host": "smtp.example.com",
            "port": "587",
            "security": "starttls",
            "username": "u",
            "from_address": "shop@example.com",
            "from_name": "Shop",
            "password": "hunter2",
            "overdue_reminder_days": "7",
        },
    )
    assert response.status_code == 302
    assert secrets.get(services.PASSWORD_KEY) == "hunter2"
    assert "hunter2" not in Credential.objects.get().ciphertext
    assert "hunter2" not in admin_client.get("/settings/email/").content.decode()
    admin_client.post("/settings/email/", {"test_to": "me@example.com"})
    assert mail.outbox[-1].subject == "Trammel test email"
    for path in ("/settings/email/log/", "/settings/email/templates/quote_sent/"):
        assert admin_client.get(path).status_code == 200


@pytest.mark.django_db
def test_staff_cannot_reach_email_settings(staff_client):
    assert staff_client.get("/settings/email/").status_code == 403
