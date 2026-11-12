"""Paid invoices marked clearly (0.10.1): a large stamp with the date paid, or
"Partly paid" with what's still due; the payments received listed on the
document; and the payment receipt carrying the invoices it paid."""

import hashlib
import importlib
from decimal import Decimal

import pytest
from django.apps import apps
from django.utils import timezone

from tests.test_billing import taxes  # noqa: F401
from tests.test_payments import _issued
from trammel.billing import services as billing
from trammel.billing.models import InvoiceTemplate
from trammel.billing.seed import DEFAULT_TEMPLATE

D = Decimal


@pytest.mark.django_db
def test_paid_partly_paid_and_the_payments_received(taxes, customer, admin):  # noqa: F811
    invoice = _issued(customer, admin)  # 99.27
    html = billing.render_html(invoice)
    assert 'class="stamp' not in html and "Payments received" not in html

    day = timezone.localdate()
    billing.record_payment(
        customer,
        amount=D("40"),
        received_on=day,
        method="check",
        reference="1042",
        allocations={invoice: D("40")},
    )
    invoice.refresh_from_db()
    context = billing.document_context(invoice)["invoice"]
    assert context["payments"] == [
        {
            "kind": "payment",
            "date": day,
            "method": "Check",
            "reference": "1042",
            "amount": D("40.00"),
        }
    ]
    assert context["paid_on"] is None
    html = billing.render_html(invoice)
    assert '<div class="stamp partial">Partly paid<small>$59.27 still due</small></div>' in html
    assert "Payments received" in html and "1042" in html

    billing.record_payment(
        customer,
        amount=D("59.27"),
        received_on=day,
        method="paypal",
        reference="TX-9",
        allocations={invoice: D("59.27")},
    )
    invoice.refresh_from_db()
    context = billing.document_context(invoice)["invoice"]
    assert context["paid_on"] == day and [p["method"] for p in context["payments"]] == [
        "Check",
        "PayPal",
    ]
    html = billing.render_html(invoice)
    assert '<div class="stamp">Paid<small>' in html and "TX-9" in html
    assert "Partly paid" not in html


@pytest.mark.django_db
def test_a_credit_note_shows_among_what_was_received(taxes, customer, admin):  # noqa: F811
    invoice = _issued(customer, admin)
    note = billing.create_credit_note(invoice, amount=D("10"), description="Scratch")
    note = billing.issue(note, actor=admin)
    invoice.refresh_from_db()
    rows = billing.document_context(invoice)["invoice"]["payments"]
    assert rows and rows[-1]["method"] == "Credit note" and rows[-1]["reference"] == note.number


@pytest.mark.django_db
def test_the_shipped_template_is_updated_and_an_edited_one_left(setup_done, monkeypatch):
    migration = importlib.import_module("trammel.billing.migrations.0013_paid_stamp")
    shipped = "<html>the old default</html>"
    monkeypatch.setattr(migration, "SHIPPED", {hashlib.sha256(shipped.encode()).hexdigest()})
    InvoiceTemplate.objects.all().delete()
    old = InvoiceTemplate.objects.create(name="Standard", body=shipped, is_default=True)
    own = InvoiceTemplate.objects.create(name="Ours", body="<html>our own</html>")
    migration.forward(apps, None)
    old.refresh_from_db()
    own.refresh_from_db()
    assert old.body == DEFAULT_TEMPLATE and own.body == "<html>our own</html>"


@pytest.mark.django_db
def test_the_receipt_carries_the_invoices_it_paid(
    taxes,  # noqa: F811
    customer,
    admin,
    django_capture_on_commit_callbacks,
):
    from trammel.core.models import BackgroundTask
    from trammel.notifications.models import SmtpSettings

    smtp = SmtpSettings.load()
    smtp.host, smtp.from_address = "smtp.example.com", "shop@example.com"
    smtp.save()
    first = _issued(customer, admin)
    second = _issued(customer, admin, "20")
    with django_capture_on_commit_callbacks(execute=True):
        billing.record_payment(
            customer,
            amount=first.balance + second.balance,
            received_on=timezone.localdate(),
            method="check",
            allocations={first: first.balance, second: second.balance},
        )
    sent = BackgroundTask.objects.filter(name="notifications.send").order_by("created_at").last()
    assert sent.payload["attachment"] == [
        {"kind": "invoice", "id": str(first.pk)},
        {"kind": "invoice", "id": str(second.pk)},
    ]
    assert f"Attached: {first.number}, {second.number}, as they stand now." in sent.payload["body"]
