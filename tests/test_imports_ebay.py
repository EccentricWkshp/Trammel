"""eBay orders (0.6.5): connecting with the RuName, reading orders with eBay's
collect-and-remit tax, account deletion requests, and the tabbed settings page."""

from __future__ import annotations

import base64
import datetime as dt
import json
import urllib.parse
from decimal import Decimal as D

import pytest
from django.test import Client
from django.utils import timezone

from tests.test_imports import coaster, module_on, tacoma  # noqa: F401 — fixtures
from trammel.accounts.models import ApiKey
from trammel.billing.models import Invoice
from trammel.core import secrets
from trammel.customers.models import Address, Customer
from trammel.imports import deletions, ebay, http, services
from trammel.imports.models import DeletionRequest, EbaySettings, ImportedOrder


def usd(value: str) -> dict:
    return {"value": value, "currency": "USD"}


def order(n=1, **over) -> dict:
    """An eBay order as the Sell Fulfillment API returns it (with TAX_BREAKDOWN)."""
    base = {
        "orderId": f"12-34567-{n:05d}",
        "creationDate": "2026-11-02T18:15:00.000Z",
        "orderPaymentStatus": "PAID",
        "cancelStatus": {"cancelState": "NONE_REQUESTED"},
        "buyer": {"username": "gracehopper1906"},
        "buyerCheckoutNotes": "Leave at the door",
        "pricingSummary": {
            "priceSubtotal": usd("80.00"),
            "priceDiscount": usd("-5.00"),
            "deliveryCost": usd("10.00"),
            "tax": usd("8.84"),
            "total": usd("93.84"),
        },
        "fulfillmentStartInstructions": [
            {
                "shippingStep": {
                    "shipTo": {
                        "fullName": "Grace Hopper",
                        "email": "abc123@members.ebay.com",
                        "primaryPhone": {"phoneNumber": "555-0100"},
                        "contactAddress": {
                            "addressLine1": "1 Navy Way",
                            "city": "Tacoma",
                            "stateOrProvince": "WA",
                            "postalCode": "98402",
                            "countryCode": "US",
                        },
                    }
                }
            }
        ],
        "lineItems": [
            {
                "title": "Walnut coaster",
                "sku": "CST-WAL",
                "quantity": 2,
                "lineItemCost": usd("80.00"),
                "variationAspects": [{"name": "Finish", "value": "Oiled"}],
                "ebayCollectAndRemitTaxes": [{"taxType": "STATE_SALES_TAX", "amount": usd("8.84")}],
            }
        ],
    }
    base.update(over)
    return base


def bring_in(*orders):
    return services.import_orders([ebay.read(o) for o in orders], "ebay")


@pytest.fixture
def ebay_app(module_on):  # noqa: F811
    config = EbaySettings.load()
    config.enabled, config.client_id, config.ru_name = True, "App-ID-1", "Eccentric-Trammel-ab"
    config.save()
    secrets.put(ebay.CERT_ID_KEY, "PRD-cert")
    return config


@pytest.fixture
def connected(ebay_app):
    EbaySettings.objects.update(
        connected_at=timezone.now(),
        access_token_expires_at=timezone.now() + dt.timedelta(hours=2),
        refresh_token_expires_at=timezone.now() + dt.timedelta(days=500),
    )
    secrets.put(ebay.ACCESS_TOKEN_KEY, "v^1.1#access")
    secrets.put(ebay.REFRESH_TOKEN_KEY, "v^1.1#refresh")
    return EbaySettings.load()


def fake_ebay(monkeypatch, answers):
    calls = []

    def fake(label, method, url, *, data=None, headers=None):
        calls.append({"method": method, "url": url, "data": data, "headers": headers or {}})
        for piece, answer in answers:
            if piece in url:
                if isinstance(answer, Exception):
                    raise answer
                return (answer() if callable(answer) else answer), {}
        raise AssertionError(f"unexpected call to {url}")

    monkeypatch.setattr(ebay, "call", fake)
    return calls


def test_reading_an_order():
    read = ebay.read(order())
    assert read["lines"] == [
        {
            "sku": "CST-WAL",
            "title": "Walnut coaster",
            "variant": "Finish: Oiled",
            "quantity": "2",
            "unit_price": "40.00",
        }
    ]
    assert (read["discount"], read["shipping"], read["tax"], read["total"]) == (
        "5.00",
        "10.00",
        "8.84",
        "93.84",
    )
    assert read["tax_remitted_by"] == "eBay" and read["buyer_id"] == "gracehopper1906"
    assert read["customer"]["region"] == "WA" and read["customer"]["phone"] == "555-0100"
    # Without the tax breakdown eBay leaves its own tax out of the total.
    without = order(pricingSummary={**order()["pricingSummary"], "total": usd("85.00")})
    assert ebay.read(without)["total"] == "93.84"
    # Washington, spelled out.
    spelled = order()
    spelled["fulfillmentStartInstructions"][0]["shippingStep"]["shipTo"]["contactAddress"][
        "stateOrProvince"
    ] = "Washington"
    assert ebay.read(spelled)["customer"]["region"] == "WA"
    assert ebay.read(order(orderPaymentStatus="PARTIALLY_REFUNDED"))["financial_status"] == (
        "partially_refunded"
    )
    assert ebay.read(order(cancelStatus={"cancelState": "CANCELED"}))["cancelled_at"]
    hidden = ebay.read(order(fulfillmentStartInstructions=[]))
    assert hidden["customer_unavailable"]


@pytest.mark.django_db
def test_an_ebay_sale_records_the_tax_as_paid_by_ebay(module_on, tacoma, coaster):  # noqa: F811
    result = bring_in(order(1), order(2, fulfillmentStartInstructions=[]))
    assert (result.created, result.pending) == (1, 1)
    sale = ImportedOrder.objects.get(order_number="12-34567-00001").invoice
    assert sale.status == "paid" and sale.total == D("93.84")
    assert sale.tax_remitted_by == "eBay" and not sale.tax_flagged
    assert sale.lines.first().product == coaster


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_connecting_ebay_with_the_runame(ebay_app, admin_client, monkeypatch):
    response = admin_client.post("/imports/settings/ebay/connect/")
    url = urllib.parse.urlparse(response["Location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://auth.ebay.com/oauth2/authorize"
    query = dict(urllib.parse.parse_qsl(url.query))
    assert query["redirect_uri"] == "Eccentric-Trammel-ab" and query["client_id"] == "App-ID-1"
    assert query["scope"] == ebay.SCOPES[0]
    calls = fake_ebay(
        monkeypatch,
        [
            (
                "/identity/v1/oauth2/token",
                {
                    "access_token": "v^1.1#new",
                    "expires_in": 7200,
                    "refresh_token": "v^1.1#refresh",
                    "refresh_token_expires_in": 47304000,
                },
            )
        ],
    )
    page = admin_client.get(
        "/imports/ebay/callback/", {"code": "v^1.1#code", "state": "forged"}, follow=True
    ).content.decode()
    assert "doesn&#x27;t match" in page and not calls
    admin_client.get("/imports/ebay/callback/", {"code": "v^1.1#code", "state": query["state"]})
    token = calls[0]
    assert token["url"] == "https://api.ebay.com/identity/v1/oauth2/token"
    assert token["data"] == {
        "grant_type": "authorization_code",
        "code": "v^1.1#code",
        "redirect_uri": "Eccentric-Trammel-ab",
    }
    basic = base64.b64decode(token["headers"]["Authorization"].split()[1]).decode()
    assert basic == "App-ID-1:PRD-cert"
    config = EbaySettings.load()
    assert config.is_connected
    assert config.refresh_token_expires_at > timezone.now() + dt.timedelta(days=540)
    page = admin_client.get("/imports/settings/?tab=ebay").content.decode()
    assert "v^1.1" not in page and "PRD-cert" not in page and "Connected since" in page


@pytest.mark.django_db
def test_pasting_ebays_encoded_code_and_the_sandbox(ebay_app, admin_client, monkeypatch):
    EbaySettings.objects.update(sandbox=True)
    response = admin_client.post("/imports/settings/ebay/connect/")
    assert response["Location"].startswith("https://auth.sandbox.ebay.com/oauth2/authorize?")
    calls = fake_ebay(monkeypatch, [("oauth2/token", {"access_token": "t", "refresh_token": "r"})])
    admin_client.post("/imports/settings/ebay/paste/", {"code": "v%5E1.1%23i%5E1"})
    assert calls[0]["url"].startswith("https://api.sandbox.ebay.com/")
    assert calls[0]["data"]["code"] == "v^1.1#i^1"
    assert EbaySettings.load().is_connected


@pytest.mark.django_db
def test_renewing_restates_the_scopes_and_a_refused_one_asks_to_reconnect(connected, monkeypatch):
    EbaySettings.objects.update(access_token_expires_at=timezone.now())
    calls = fake_ebay(monkeypatch, [("oauth2/token", {"access_token": "fresh"})])
    assert ebay.access_token(EbaySettings.load()) == "fresh"
    assert calls[0]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "v^1.1#refresh",
        "scope": ebay.SCOPES[0],
    }
    EbaySettings.objects.update(access_token_expires_at=timezone.now())
    fake_ebay(monkeypatch, [("oauth2/token", http.HttpProblem(400, "expired", "invalid_grant"))])
    with pytest.raises(ebay.EbayError, match="Connect eBay again"):
        ebay.access_token(EbaySettings.load())


@pytest.mark.django_db
def test_fetching_pages_with_the_tax_breakdown(connected, monkeypatch):
    pages = [{"orders": [order(n) for n in range(200)]}, {"orders": [order(200)]}]
    calls = fake_ebay(monkeypatch, [("/sell/fulfillment/v1/order", lambda: pages.pop(0))])
    found = ebay.fetch(EbaySettings.load(), dt.date(2026, 11, 1), dt.date(2026, 11, 7))
    assert len(found) == 201
    second = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(calls[1]["url"]).query))
    assert second["offset"] == "200" and second["fieldGroups"] == "TAX_BREAKDOWN"
    assert second["filter"].startswith("creationdate:[2026-11-01T")
    assert second["filter"].endswith(".999Z]")
    assert calls[0]["headers"]["Authorization"] == "Bearer v^1.1#access"


@pytest.mark.django_db
def test_fetching_ebay_from_the_page(connected, tacoma, admin_client, monkeypatch):  # noqa: F811
    fake_ebay(monkeypatch, [("fulfillment", {"orders": [order(1)]})])
    response = admin_client.post(
        "/imports/fetch/",
        {"source": "ebay", "start": "2026-11-01", "end": "2026-11-07"},
        follow=True,
    )
    assert "eBay: 1 new sale" in response.content.decode()


# ---------------------------------------------------------------------------
# The settings page's tabs
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_online_stores_has_a_tab_per_store(module_on, admin_client):  # noqa: F811
    page = admin_client.get("/imports/settings/").content.decode()
    assert 'id="shopify-heading"' in page and 'id="etsy-heading"' not in page
    assert "?tab=etsy" in page and "?tab=ebay" in page
    page = admin_client.get("/imports/settings/?tab=ebay").content.decode()
    assert 'id="ebay-heading"' in page and 'id="shopify-heading"' not in page
    assert "/imports/ebay/callback/" in page
    # A form that comes back with an error opens on its own tab.
    page = admin_client.post(
        "/imports/settings/ebay/", {"ebay-enabled": "on", "ebay-client_id": ""}
    ).content.decode()
    assert 'id="ebay-heading"' in page and "eBay needs this to connect." in page
    # Saving returns to the store's own tab.
    response = admin_client.post(
        "/imports/settings/ebay/",
        {"ebay-enabled": "on", "ebay-client_id": "App", "ebay-ru_name": "Ru", "ebay-cert_id": "c"},
    )
    assert response["Location"].endswith("?tab=ebay")
    assert secrets.get(ebay.CERT_ID_KEY) == "c"
    response = admin_client.post("/imports/settings/etsy/", {"etsy-keystring": "k"})
    assert response["Location"].endswith("?tab=etsy")


# ---------------------------------------------------------------------------
# Account deletion requests (FR-IMP-6)
# ---------------------------------------------------------------------------


def notice(username="gracehopper1906", notification_id="n-1") -> dict:
    return {
        "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION", "schemaVersion": "1.0"},
        "notification": {
            "notificationId": notification_id,
            "eventDate": "2026-11-10T20:00:00.000Z",
            "publishDate": "2026-11-10T20:00:05.000Z",
            "publishAttemptCount": 1,
            "data": {"username": username, "userId": "ma8vp1jySJC", "eiasToken": "nY+sHZ2"},
        },
    }


@pytest.fixture
def relay(admin):
    _, token = ApiKey.issue(admin, "eBay relay", ["imports.deletion_notices"])
    return Client(HTTP_AUTHORIZATION=f"Bearer {token}")


def post_notice(client, body):
    return client.post(
        "/api/v1/imports/ebay/deletion-notices", json.dumps(body), content_type="application/json"
    )


@pytest.mark.django_db
def test_a_forwarded_notice_wipes_the_orders_and_waits_for_review(
    module_on,  # noqa: F811
    tacoma,  # noqa: F811
    relay,
    admin_client,
):
    bring_in(order(1))
    customer = ImportedOrder.objects.get().customer
    response = post_notice(relay, notice())
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["status"] == "open" and body["orders"] == 1 and not body["duplicate"]
    stored = ImportedOrder.objects.get()
    assert stored.payload["customer"]["name"] == "" and stored.payload["customer"]["line1"] == ""
    # The customer record itself is untouched until someone reviews it.
    customer.refresh_from_db()
    assert customer.name == "Grace Hopper"
    # The same notice again does nothing twice.
    again = post_notice(relay, notice()).json()
    assert again["duplicate"] and DeletionRequest.objects.count() == 1
    # Someone who never bought from you is closed at once.
    assert post_notice(relay, notice("stranger", "n-2")).json()["status"] == "no_match"
    page = admin_client.get("/imports/").content.decode()
    assert "1 request from eBay to delete a buyer&#x27;s data is waiting for review." in page
    review = admin_client.get("/imports/deletions/").content.decode()
    assert "eBay user gracehopper1906" in review and "1 eBay order only" in review


@pytest.mark.django_db
def test_the_notice_api_needs_its_own_permission(module_on, admin):  # noqa: F811
    _, token = ApiKey.issue(admin, "reader", ["imports.view"])
    client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert post_notice(client, notice()).status_code == 403
    assert post_notice(Client(), notice()).status_code == 401


@pytest.mark.django_db
def test_anonymizing_keeps_the_money(module_on, tacoma, admin_client):  # noqa: F811
    bring_in(order(1))
    row, _ = deletions.receive(username="gracehopper1906")
    sale = Invoice.objects.get()
    customer = sale.customer
    # The same person also bought directly: the review page says so.
    from trammel.billing import services as billing

    direct = billing.create(customer, kind="direct_sale")
    assert "1 other invoice" in admin_client.get("/imports/deletions/").content.decode()
    direct.delete()
    response = admin_client.post(f"/imports/deletions/{row.pk}/anonymize/{customer.pk}/")
    assert response.status_code == 302
    customer.refresh_from_db()
    assert (
        customer.name.startswith("Deleted buyer") and customer.email == "" and customer.phone == ""
    )
    address = Address.objects.get(customer=customer)
    assert address.line1 == "(deleted)" and address.postal_code == "98402"
    sale.refresh_from_db()
    assert sale.total == D("93.84") and sale.ship_to_text == "" and sale.tax_location_code == "2717"
    row.refresh_from_db()
    assert row.status == DeletionRequest.Status.DONE and row.resolved_at
    assert Customer.objects.filter(name="Grace Hopper").count() == 0


@pytest.mark.django_db
def test_dismissing_needs_a_reason(module_on, tacoma, admin_client):  # noqa: F811
    bring_in(order(1))
    row, _ = deletions.receive(username="gracehopper1906")
    admin_client.post(f"/imports/deletions/{row.pk}/dismiss/", {"note": ""})
    row.refresh_from_db()
    assert row.status == DeletionRequest.Status.OPEN
    admin_client.post(f"/imports/deletions/{row.pk}/dismiss/", {"note": "Also a local customer"})
    row.refresh_from_db()
    assert row.status == DeletionRequest.Status.DISMISSED and row.note == "Also a local customer"
    assert Customer.objects.filter(name="Grace Hopper").exists()
    response = admin_client.post("/imports/deletions/new/", {"username": "", "user_id": ""})
    assert response.status_code == 302 and DeletionRequest.objects.count() == 1
