"""Online orders from Shopify (FR-IMP, ported from sales_tracker): reading them,
bringing them in once as paid sales at the store's figures, orders waiting for
a customer, jobs from orders, the connection and the webhook."""

import base64
import datetime as dt
import hashlib
import hmac
import json
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from tests.conftest import make_user
from trammel.billing.models import Invoice, InvoiceTax, Payment
from trammel.catalog import services as catalog
from trammel.catalog.models import Product
from trammel.core import modules, secrets, tasks
from trammel.customers.models import Address, Contact, Customer
from trammel.imports import services, shopify
from trammel.imports.models import ImportedOrder, ShopifySettings
from trammel.tax.models import Boundary, Jurisdiction, TaxRate

D = Decimal


def order(n=1001, **over) -> dict:
    """A Shopify order as the Admin API returns it."""
    base = {
        "id": 450789469 + n,
        "name": f"#{n}",
        "order_number": n,
        "created_at": "2026-11-02T10:15:00-08:00",
        "email": "grace@example.com",
        "financial_status": "paid",
        "test": False,
        "cancelled_at": None,
        "note": "Gift wrap, please.",
        "total_discounts": "5.00",
        "total_tax": "8.84",
        "total_price": "93.84",
        "taxes_included": False,
        "total_outstanding": "0.00",
        "total_shipping_price_set": {"shop_money": {"amount": "10.00"}},
        "customer": {"id": 207119551, "first_name": "Grace", "last_name": "Hopper"},
        "shipping_address": {
            "name": "Grace Hopper",
            "address1": "1 Navy Way",
            "city": "Tacoma",
            "province_code": "WA",
            "zip": "98402",
            "country_code": "US",
            "phone": "555-0100",
        },
        "line_items": [
            {"title": "Walnut coaster", "sku": "CST-WAL", "quantity": 2, "price": "40.00"},
            {"title": "Gift card", "sku": "", "quantity": 0, "price": "25.00"},
        ],
        "fulfillments": [
            {
                "tracking_number": "1Z999",
                "tracking_company": "UPS",
                "created_at": "2026-11-03T09:00:00-08:00",
            }
        ],
    }
    base.update(over)
    return base


@pytest.fixture
def module_on(setup_done):
    modules.set_enabled("imports", True)


@pytest.fixture
def tacoma(setup_done):
    j = Jurisdiction.objects.create(code="2717", name="Tacoma", region="WA")
    TaxRate.objects.create(jurisdiction=j, rate=D("10.40"), effective_from=dt.date(2026, 1, 1))
    Boundary.objects.create(
        zip_low="984020000",
        zip_high="984099999",
        jurisdiction_codes=["2717"],
        location_code="2717",
        source="manual",
    )
    return j


@pytest.fixture
def coaster(setup_done):
    return catalog.save_product(Product(name="Walnut coaster", sku="CST-WAL", price=D("40")))


def bring_in(*orders):
    return services.import_orders([shopify.read(o) for o in orders], "shopify")


def test_reading_an_order():
    read = shopify.read(order())
    assert read["order_number"] == "#1001" and read["external_order_id"] == "450790470"
    assert read["customer"]["region"] == "WA" and read["customer"]["name"] == "Grace Hopper"
    assert [line["quantity"] for line in read["lines"]] == ["2"]  # nothing ordered, no line
    assert (read["shipping"], read["tax"], read["discount"]) == ("10.00", "8.84", "5.00")
    assert (read["carrier"], read["tracking"], read["shipped_on"]) == (
        "UPS",
        "1Z999",
        dt.date(2026, 11, 3),
    )
    assert read["buyer_id"] == "207119551" and not read["customer_unavailable"]
    hidden = shopify.read(order(email=None, shipping_address=None, customer={"id": 5}))
    assert hidden["customer_unavailable"]


@pytest.mark.django_db
def test_an_order_becomes_a_paid_sale_at_the_stores_figures(module_on, tacoma, coaster):
    result = bring_in(order())
    assert (result.created, result.errors) == (1, [])
    row = ImportedOrder.objects.get()
    invoice = row.invoice
    assert invoice.kind == Invoice.Kind.DIRECT_SALE and invoice.status == "paid"
    assert invoice.customer.email == "grace@example.com"
    assert invoice.ship_to.postal_code == "98402" and invoice.tax_location_code == "2717"
    lines = list(invoice.lines.order_by("position"))
    assert lines[0].product == coaster and lines[0].line_total == D("80.00")
    assert lines[1].line_total == D("-5.00")  # the discount
    assert (invoice.shipping, invoice.tax_total, invoice.total) == (
        D("10.00"),
        D("8.84"),
        D("93.84"),
    )
    assert sum(t.amount for t in InvoiceTax.objects.filter(invoice=invoice)) == D("8.84")
    assert not invoice.tax_flagged and not row.problem
    payment = Payment.objects.get()
    assert (payment.method, payment.amount, payment.reference) == (
        "online",
        D("93.84"),
        "Shopify #1001",
    )
    assert (row.tracking, row.carrier, row.buyer_id) == ("1Z999", "UPS", "207119551")


@pytest.mark.django_db
def test_tax_that_differs_from_trammels_rates_is_kept_and_flagged(module_on, tacoma):
    bring_in(order(total_tax="7.80", total_price="92.80"))
    invoice = ImportedOrder.objects.get().invoice
    assert invoice.tax_total == D("7.80") and invoice.total == D("92.80")
    assert invoice.tax_flagged and "Shopify collected 7.80" in invoice.tax_note
    assert sum(t.amount for t in invoice.taxes.all()) == D("7.80")


@pytest.mark.django_db
def test_tax_with_no_location_is_kept_and_flagged(module_on):
    bring_in(order())
    invoice = ImportedOrder.objects.get().invoice
    assert invoice.tax_total == D("8.84") and invoice.tax_flagged
    assert [t.name for t in invoice.taxes.all()] == ["Collected by Shopify"]


@pytest.mark.django_db
def test_nothing_is_imported_twice_and_later_news_is_noted(module_on, tacoma):
    bring_in(order())
    invoice = ImportedOrder.objects.get().invoice
    result = bring_in(
        order(
            financial_status="refunded",
            fulfillments=[{"tracking_number": "1Z000", "tracking_company": "FedEx Ground"}],
        )
    )
    assert (result.created, result.updated) == (0, 1)
    assert (
        Invoice.objects.count() == 1
        and Customer.objects.filter(email="grace@example.com").count() == 1
    )
    row = ImportedOrder.objects.get()
    assert row.tracking == "1Z000" and "credit note" in row.problem
    invoice.refresh_from_db()
    assert invoice.total == D("93.84")  # an issued sale never changes
    bring_in(order(cancelled_at="2026-11-04T10:00:00-08:00"))
    row.refresh_from_db()
    assert row.cancelled_at and "Canceled in Shopify" in row.problem


@pytest.mark.django_db
def test_test_and_canceled_orders_are_skipped(module_on):
    result = bring_in(order(1, test=True), order(2, cancelled_at="2026-11-02T11:00:00Z"))
    assert result.skipped == 2 and not ImportedOrder.objects.exists()


@pytest.mark.django_db
def test_customers_are_matched_by_email_and_left_as_they_are(module_on, tacoma):
    known = Customer.objects.create(name="Rear Admiral Hopper", email="GRACE@example.com")
    other = Customer.objects.create(name="Navy Yard")
    Contact.objects.create(customer=other, name="Ada", email="ada@example.com")
    bring_in(order(1), order(2, email="ada@example.com"))
    first, second = ImportedOrder.objects.order_by("order_number")
    assert first.customer == known and second.customer == other
    known.refresh_from_db()
    assert known.name == "Rear Admiral Hopper"  # never overwritten
    assert Address.objects.filter(customer=known, postal_code="98402").exists()


@pytest.mark.django_db
def test_an_order_without_its_buyer_waits_for_a_customer(module_on, admin_client):
    hidden = order(
        email=None,
        customer={"id": 9},
        shipping_address={"province_code": "WA", "country_code": "US"},
    )
    assert bring_in(hidden).pending == 1
    row = ImportedOrder.objects.get()
    assert row.status == "pending" and row.invoice is None and not Invoice.objects.exists()
    page = admin_client.get("/imports/").content.decode()
    assert "Waiting for a customer" in page and "WA" in page
    response = admin_client.post(
        f"/imports/{row.pk}/complete/",
        {
            "customer": "",
            "name": "Linus Torvalds",
            "email": "linus@example.com",
            "line1": "2 Penguin Rd",
            "city": "Tacoma",
            "region": "WA",
            "postal_code": "98402",
            "country": "US",
        },
    )
    row.refresh_from_db()
    assert response.status_code == 302 and row.status == "imported"
    assert row.invoice.customer.name == "Linus Torvalds" and row.invoice.status == "paid"
    assert row.invoice.ship_to.line1 == "2 Penguin Rd"
    # Fetching again doesn't bring it back as waiting, or make a second sale.
    bring_in(hidden)
    assert Invoice.objects.count() == 1


@pytest.mark.django_db
def test_a_waiting_order_can_go_to_an_existing_customer_or_be_discarded(module_on, admin_client):
    hidden = order(email=None, customer={"id": 9}, shipping_address=None)
    bring_in(hidden, order(1002, email=None, customer={"id": 9}, shipping_address=None))
    first, second = ImportedOrder.objects.order_by("order_number")
    known = Customer.objects.create(name="Grace Hopper", email="grace@example.com")
    admin_client.post(f"/imports/{first.pk}/complete/", {"customer": str(known.pk)})
    first.refresh_from_db()
    assert first.invoice.customer == known
    admin_client.post(f"/imports/{second.pk}/discard/")
    assert not ImportedOrder.objects.filter(pk=second.pk).exists()


@pytest.mark.django_db
def test_making_a_job_from_an_order(module_on, tacoma, coaster, admin_client):
    bring_in(order())
    row = ImportedOrder.objects.get()
    page = admin_client.get(row.invoice.get_absolute_url()).content.decode()
    assert "Shopify order #1001" in page and "Make a job" in page
    response = admin_client.post(f"/imports/{row.pk}/job/")
    row.refresh_from_db()
    job = row.job
    assert response["Location"] == job.get_absolute_url()
    assert job.status == "approved" and job.customer == row.invoice.customer
    revision = job.quotes.get().revisions.get()
    assert revision.status == "approved" and revision.approval.method == "online"
    assert [ln.product for ln in revision.lines.order_by("position")][0] == coaster
    from trammel.agreements import services as agreements

    assert agreements.satisfied(job)
    row.invoice.refresh_from_db()
    assert row.invoice.job == job
    # The sale already bills the quote's lines.
    from trammel.billing.services import _already_billed

    assert set(revision.lines.values_list("pk", flat=True)) <= _already_billed("quote_line")
    with pytest.raises(ValidationError):
        services.make_job(row)


@pytest.mark.django_db
def test_fetching_from_the_page(module_on, tacoma, admin_client, monkeypatch):
    config = ShopifySettings.load()
    config.enabled, config.shop_domain = True, "eccentric.myshopify.com"
    config.save()
    asked = {}

    def fake_fetch(cfg, start, end):
        asked["range"] = (start, end)
        return [order(1001), order(1002)]

    monkeypatch.setattr(shopify, "fetch", fake_fetch)
    response = admin_client.post(
        "/imports/fetch/", {"start": "2026-11-01", "end": "2026-11-07"}, follow=True
    )
    assert asked["range"] == (dt.date(2026, 11, 1), dt.date(2026, 11, 7))
    assert "Shopify: 2 new sales" in response.content.decode()
    assert ImportedOrder.objects.count() == 2


def _respond(monkeypatch, pages):
    """Fake Shopify: each call returns the next (payload, headers)."""
    calls = []

    def fake(method, url, *, data=None, headers=None):
        calls.append((method, url, data, headers))
        return pages.pop(0)

    monkeypatch.setattr(shopify, "_request", fake)
    return calls


@pytest.mark.django_db
def test_fetching_follows_pages_with_the_token(module_on, monkeypatch):
    config = ShopifySettings.load()
    config.enabled, config.shop_domain = True, "eccentric.myshopify.com"
    config.save()
    secrets.put(shopify.TOKEN_KEY, "shpat_abc")
    next_url = (
        "https://eccentric.myshopify.com/admin/api/2026-01/orders.json?page_info=xyz&limit=250"
    )
    calls = _respond(
        monkeypatch,
        [
            ({"orders": [order(1)]}, {"Link": f'<{next_url}>; rel="next"'}),
            ({"orders": [order(2)]}, {}),
        ],
    )
    orders = shopify.fetch(config, dt.date(2026, 11, 1), dt.date(2026, 11, 7))
    assert [o["name"] for o in orders] == ["#1", "#2"]
    assert calls[0][3]["X-Shopify-Access-Token"] == "shpat_abc"
    assert "created_at_min=2026-11-01T00%3A00%3A00" in calls[0][1] and calls[1][1] == next_url


@pytest.mark.django_db
def test_the_client_secret_route_checks_and_keeps_its_token(module_on, monkeypatch):
    config = ShopifySettings.load()
    config.enabled, config.shop_domain = True, "eccentric.myshopify.com"
    config.auth_mode, config.client_id = "client_credentials", "client-1"
    config.save()
    secrets.put(shopify.CLIENT_SECRET_KEY, "s3cret")
    _respond(monkeypatch, [({"access_token": "shpat_x", "scope": ""}, {})])
    with pytest.raises(shopify.ShopifyError, match="read_orders"):
        shopify.access_token(config)
    calls = _respond(
        monkeypatch,
        [({"access_token": "shpat_new", "scope": "read_orders,read_customers"}, {})],
    )
    assert shopify.access_token(config) == "shpat_new"
    config.refresh_from_db()
    assert shopify.access_token(config) == "shpat_new" and len(calls) == 1  # kept, not refetched
    assert calls[0][2]["grant_type"] == "client_credentials"


def test_only_a_myshopify_address_is_accepted():
    assert (
        shopify.check_domain("https://Eccentric.myshopify.com/admin") == "eccentric.myshopify.com"
    )
    for bad in ("eccentricworkshop.com", "192.168.1.1", "evil.com/x.myshopify.com", ""):
        with pytest.raises(ValidationError):
            shopify.check_domain(bad)


@pytest.mark.django_db
def test_nothing_goes_out_while_outbound_traffic_is_off(module_on):
    from trammel.core.models import SystemSettings

    system = SystemSettings.load()
    system.offline_mode = True
    system.save()
    with pytest.raises(shopify.ShopifyError, match="switched off"):
        shopify._request("GET", "https://eccentric.myshopify.com/admin/api/x")


def _signed(body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


@pytest.mark.django_db
def test_webhooks_are_off_until_switched_on_and_must_be_signed(module_on, tacoma):
    url = "/imports/shopify/webhook/"
    body = json.dumps(order()).encode()
    post = Client().post
    assert post(url, body, content_type="application/json").status_code == 404
    config = ShopifySettings.load()
    config.enabled, config.webhooks_enabled = True, True
    config.shop_domain = "eccentric.myshopify.com"
    config.save()
    assert post(url, body, content_type="application/json").status_code == 404  # no secret
    secrets.put(shopify.API_SECRET_KEY, "whsec")
    headers = {"HTTP_X_SHOPIFY_SHOP_DOMAIN": "eccentric.myshopify.com"}
    bad = post(
        url, body, content_type="application/json", HTTP_X_SHOPIFY_HMAC_SHA256="nope", **headers
    )
    assert bad.status_code == 401
    elsewhere = post(
        url,
        body,
        content_type="application/json",
        HTTP_X_SHOPIFY_HMAC_SHA256=_signed(body, "whsec"),
        HTTP_X_SHOPIFY_SHOP_DOMAIN="other.myshopify.com",
    )
    assert elsewhere.status_code == 401
    good = post(
        url,
        body,
        content_type="application/json",
        HTTP_X_SHOPIFY_HMAC_SHA256=_signed(body, "whsec"),
        **headers,
    )
    assert good.status_code == 200 and not ImportedOrder.objects.exists()
    tasks.run_pending()
    assert ImportedOrder.objects.get().invoice.total == D("93.84")


@pytest.mark.django_db
def test_connecting_shopify_keeps_secrets_out_of_sight(module_on, admin_client):
    response = admin_client.post(
        "/imports/settings/",
        {
            "enabled": "on",
            "shop_domain": "https://eccentric.myshopify.com/",
            "auth_mode": "token",
            "client_id": "",
            "token": "shpat_supersecret",
        },
    )
    assert response.status_code == 302
    config = ShopifySettings.load()
    assert config.shop_domain == "eccentric.myshopify.com"
    assert secrets.get(shopify.TOKEN_KEY) == "shpat_supersecret"
    page = admin_client.get("/imports/settings/").content.decode()
    assert "shpat_supersecret" not in page and "Saved; leave blank to keep it." in page
    assert 'type="password"' not in page and 'name="token" autocomplete="off"' in page
    bad = admin_client.post(
        "/imports/settings/",
        {"enabled": "on", "shop_domain": "mystore.com", "auth_mode": "token", "token": "shpat_x1"},
    )
    assert "myshopify.com address" in bad.content.decode()
    assert "shpat_x1" not in bad.content.decode()


@pytest.mark.django_db
def test_an_online_stores_buyers_are_never_emailed(
    module_on, tacoma, django_capture_on_commit_callbacks
):
    from django.core import mail

    from trammel.notifications.models import EmailLog, SmtpSettings
    from trammel.notifications.tasks import overdue_reminders

    smtp = SmtpSettings.load()
    smtp.host, smtp.from_address = "smtp.example.com", "shop@example.com"
    smtp.save()
    with django_capture_on_commit_callbacks(execute=True):
        # One paid order, one the store hasn't been paid for yet (a balance due on
        # the order date, which has passed), then news of a refund on the first.
        bring_in(order(1), order(2, email="ada@example.com", financial_status="pending"))
        bring_in(order(1, financial_status="partially_refunded"))
        overdue_reminders({})
    tasks.run_pending()
    assert Invoice.objects.filter(imported_order__isnull=False).count() == 2
    assert Invoice.objects.get(imported_order__order_number="#2").balance > 0
    assert not EmailLog.objects.exists() and not mail.outbox


def _group_hidden(page, mode):
    start = page.index(f'data-shown-for="auth_mode={mode}"')
    return page[start : page.index(">", start)].endswith("hidden")


@pytest.mark.django_db
def test_only_the_chosen_sign_in_routes_fields_show(module_on, admin_client):
    page = admin_client.get("/imports/settings/").content.decode()
    assert not _group_hidden(page, "token") and _group_hidden(page, "client_credentials")
    config = ShopifySettings.load()
    config.auth_mode = ShopifySettings.Auth.CLIENT
    config.save()
    page = admin_client.get("/imports/settings/").content.decode()
    assert _group_hidden(page, "token") and not _group_hidden(page, "client_credentials")
    assert 'name="client_id"' in page and 'name="token"' in page


@pytest.mark.django_db
def test_permissions_and_the_module_switch(module_on):
    viewer = make_user("v@example.com", "imports.view")
    client = Client()
    client.force_login(viewer)
    assert client.get("/imports/").status_code == 200
    assert client.post("/imports/fetch/", {}).status_code == 403
    assert client.get("/imports/settings/").status_code == 403
    modules.set_enabled("imports", False)
    assert client.get("/imports/").status_code == 404
    assert Client().post("/imports/shopify/webhook/", b"{}", content_type="x").status_code == 404
