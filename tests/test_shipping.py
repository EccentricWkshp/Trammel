"""Shipping charges, the combined tax line, and printable documents."""

import datetime as dt
import json
import re
from decimal import Decimal

import pytest

from tests.test_billing import draft, taxes  # noqa: F401 — fixtures
from trammel.billing import services
from trammel.billing.models import Invoice
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.reports import services as reports
from trammel.tax.models import TaxCategory, TaxSettings

D = Decimal
YEAR = (dt.date(2020, 1, 1), dt.date(2100, 1, 1))


def _mode(mode):
    settings = TaxSettings.load()
    settings.shipping_tax = mode
    settings.save()


@pytest.mark.django_db
def test_shipping_is_taxed_in_proportion_to_taxable_items(draft, admin):  # noqa: F811
    exempt = TaxCategory.objects.create(name="Design (not taxed)", taxable=False)
    services.add_line(
        draft, description="Design", quantity=1, unit_price=D("30"), tax_category=exempt
    )
    services.set_shipping(draft, D("20"))
    draft.refresh_from_db()
    # 90 of 120 is taxable, so 15 of the 20 shipping is: (90 + 15) × 10.3% = 10.82.
    assert draft.shipping_taxable == D("15.00") and draft.taxable_total == D("105.00")
    assert draft.tax_total == D("10.82") and draft.total == D("150.82")
    issued = services.issue(draft, actor=admin)
    assert issued.total == D("150.82") and issued.tax_label == "Sales tax (10.3%)"


@pytest.mark.django_db
@pytest.mark.parametrize(("mode", "taxed"), [("always", "20.00"), ("never", "0.00")])
def test_shipping_tax_setting(draft, mode, taxed):  # noqa: F811
    _mode(mode)
    services.set_shipping(draft, D("20"))
    draft.refresh_from_db()
    assert draft.shipping_taxable == D(taxed)


@pytest.mark.django_db
def test_negative_shipping_is_refused(draft):  # noqa: F811
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        services.set_shipping(draft, D("-1"))


@pytest.fixture
def shipped_quote(taxes, customer, admin):  # noqa: F811
    job = jobs.create_job(Job(title="Sign", customer=customer))
    rev = quotes.create_quote(job)
    form = LINE_FORMS["adhoc"](
        {"description": "Sign", "quantity": "1", "unit": "each", "unit_price": "1000"}
    )
    assert form.is_valid()
    quotes.save_line(form.build(rev))
    before = {}
    rev.shipping = D("50")
    quotes.update_revision(rev, before=before)
    rev.refresh_from_db()
    return job, rev


@pytest.mark.django_db
def test_quote_shipping_totals_and_carries_to_the_invoice_once(shipped_quote, customer, admin):
    job, rev = shipped_quote
    assert rev.tax_label == "Sales tax (10.3%)"
    assert rev.tax_amount == D("108.15") and rev.total == D("1158.15")  # (1000 + 50) × 10.3%
    quotes.send(rev, actor=admin)
    quotes.approve(rev, actor=admin, approved_by_name="Grace", accept_agreement=True, user=admin)
    first = services.create(customer, kind="final", job=job)
    services.add_from_quote(first, rev)
    first.refresh_from_db()
    assert first.shipping == D("50") and first.total == D("1158.15")
    services.issue(first, actor=admin)
    second = services.create(customer, kind="final", job=job)
    services.add_from_quote(second, rev)
    second.refresh_from_db()
    assert second.shipping == 0
    context = services.document_context(first)["invoice"]
    assert context["shipping"] == D("50") and context["tax_label"] == "Sales tax (10.3%)"
    html = services.render_html(first)
    assert "Shipping" in html and "Sales tax (10.3%)" in html and "2717" not in html


@pytest.mark.django_db
def test_reports_count_shipping(draft, admin):  # noqa: F811
    services.set_shipping(draft, D("20"))
    services.issue(draft, actor=admin)
    tax = reports.tax_report(*YEAR)
    assert tax["totals"]["gross"] == D("110.00") and tax["totals"]["taxable"] == D("110.00")
    assert sum(row["basis"] for row in tax["bo"]) == D("110.00")
    rows, total = reports.sales(*YEAR, group="product")
    assert dict(rows)["Shipping and delivery"] == D("20.00") and total == D("110.00")


@pytest.mark.django_db
def test_invoice_page_shows_one_tax_line_and_print_options(admin_client, draft, admin):  # noqa: F811
    url = f"/invoices/{draft.pk}/"
    admin_client.post(f"{url}shipping/", {"shipping": "12.50"})
    draft.refresh_from_db()
    assert draft.shipping == D("12.50")
    services.issue(draft, actor=admin)
    page = admin_client.get(url).content.decode()
    assert "Sales tax (10.3%)" in page and "Tacoma (2717" not in page
    assert f"{url}document/?print=1" in page and f"{url}pdf/" in page
    details = admin_client.get(f"{url}?tab=details").content.decode()
    assert "Tacoma" in details and "2717" in details

    plain = admin_client.get(f"{url}document/")
    assert "<script" not in plain.content.decode()
    printed = admin_client.get(f"{url}document/?print=1")
    nonce = re.search(r"'nonce-([^']+)'", printed["Content-Security-Policy"]).group(1)
    assert f'<script nonce="{nonce}">' in printed.content.decode()
    assert "allow-modals" in printed["Content-Security-Policy"]


@pytest.mark.django_db
def test_api_sets_shipping_on_drafts(admin_client, draft, shipped_quote):  # noqa: F811
    response = admin_client.patch(
        f"/api/v1/invoices/{draft.pk}",
        json.dumps({"shipping": "8", "fulfillment": "shipping"}),
        content_type="application/json",
    ).json()
    assert response["shipping"] == "8.00" and response["fulfillment"] == "shipping"
    _, rev = shipped_quote
    response = admin_client.patch(
        f"/api/v1/quotes/{rev.pk}", json.dumps({"shipping": "75"}), content_type="application/json"
    ).json()
    assert response["shipping"] == "75.00" and response["total"] == "1185.73"


@pytest.mark.django_db
def test_issued_invoice_shipping_cannot_change(draft, admin):  # noqa: F811
    from django.core.exceptions import ValidationError

    invoice = services.issue(draft, actor=admin)
    with pytest.raises(ValidationError):
        services.set_shipping(invoice, D("5"))
    assert Invoice.objects.get(pk=invoice.pk).shipping == 0
