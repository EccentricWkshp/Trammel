"""Etsy orders (0.6.4): connecting with OAuth and PKCE, reading receipts, and
sales whose tax Etsy collects and pays as the marketplace facilitator."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import urllib.parse
from decimal import Decimal as D

import pytest
from django.core import mail
from django.utils import timezone

from tests.test_imports import coaster, module_on, tacoma  # noqa: F401 — fixtures
from trammel.billing.models import Invoice, Payment
from trammel.core import secrets
from trammel.imports import etsy, http, services
from trammel.imports.models import EtsySettings, ImportedOrder
from trammel.notifications.models import EmailLog
from trammel.reports.services import tax_report


def cents(amount: int) -> dict:
    return {"amount": amount, "divisor": 100, "currency_code": "USD"}


def receipt(n=1, **over) -> dict:
    """An Etsy receipt as the Open API v3 returns it."""
    base = {
        "receipt_id": 3000000000 + n,
        "buyer_user_id": 7001,
        "buyer_email": "",
        "name": "Grace Hopper",
        "first_line": "1 Navy Way",
        "second_line": "",
        "city": "Tacoma",
        "state": "Washington",
        "zip": "98402",
        "country_iso": "US",
        "status": "Paid",
        "is_paid": True,
        "is_gift": False,
        "message_from_buyer": "Thank you!",
        "created_timestamp": int(dt.datetime(2026, 11, 2, 18, 15, tzinfo=dt.UTC).timestamp()),
        "updated_timestamp": int(dt.datetime(2026, 11, 3, 18, 0, tzinfo=dt.UTC).timestamp()),
        "grandtotal": cents(9384),
        "total_price": cents(8000),
        "discount_amt": cents(500),
        "total_shipping_cost": cents(1000),
        "total_tax_cost": cents(884),
        "total_vat_cost": cents(0),
        "gift_wrap_price": cents(0),
        "shipments": [
            {
                "carrier_name": "USPS",
                "tracking_code": "9400111",
                "shipment_notification_timestamp": int(
                    dt.datetime(2026, 11, 3, 17, 0, tzinfo=dt.UTC).timestamp()
                ),
            }
        ],
        "transactions": [
            {
                "title": "Walnut coaster",
                "sku": "CST-WAL",
                "quantity": 2,
                "price": cents(4000),
                "variations": [{"formatted_name": "Finish", "formatted_value": "Oiled"}],
            }
        ],
    }
    base.update(over)
    return base


def bring_in(*receipts):
    return services.import_orders([etsy.read(r) for r in receipts], "etsy")


@pytest.fixture
def connected(module_on):  # noqa: F811
    config = EtsySettings.load()
    config.enabled, config.keystring = True, "keystr1ng"
    config.shop_id, config.shop_name = "555", "Eccentric Workshop"
    config.connected_at = timezone.now()
    config.access_token_expires_at = timezone.now() + dt.timedelta(hours=1)
    config.refresh_token_expires_at = timezone.now() + dt.timedelta(days=90)
    config.save()
    secrets.put(etsy.SHARED_SECRET_KEY, "sh4red")
    secrets.put(etsy.ACCESS_TOKEN_KEY, "7001.access")
    secrets.put(etsy.REFRESH_TOKEN_KEY, "7001.refresh")
    return config


def fake_etsy(monkeypatch, answers):
    """Fake Etsy: each call gets the answer for the first matching URL piece."""
    calls = []

    def fake(method, url, *, data=None, headers=None):
        calls.append({"method": method, "url": url, "data": data, "headers": headers or {}})
        for piece, answer in answers:
            if piece in url:
                if isinstance(answer, Exception):
                    raise answer
                return (answer() if callable(answer) else answer), {}
        raise AssertionError(f"unexpected call to {url}")

    monkeypatch.setattr(etsy, "call", lambda label, method, url, **kw: fake(method, url, **kw))
    return calls


def test_reading_a_receipt():
    read = etsy.read(receipt(gift_wrap_price=cents(300), is_gift=True, gift_message="Enjoy"))
    assert read["customer"]["region"] == "WA" and read["customer"]["postal_code"] == "98402"
    assert read["lines"][0] == {
        "sku": "CST-WAL",
        "title": "Walnut coaster",
        "variant": "Finish: Oiled",
        "quantity": "2",
        "unit_price": "40.00",
    }
    assert read["lines"][1]["title"] == "Gift wrap" and read["lines"][1]["unit_price"] == "3.00"
    assert (read["discount"], read["shipping"], read["tax"], read["total"]) == (
        "5.00",
        "10.00",
        "8.84",
        "93.84",
    )
    assert read["tax_remitted_by"] == "Etsy" and read["buyer_id"] == "7001"
    assert read["financial_status"] == "paid" and read["carrier"] == "USPS"
    assert "Gift message: Enjoy" in read["notes"]
    assert etsy.read(receipt(status="Partially Refunded"))["financial_status"] == (
        "partially_refunded"
    )
    assert etsy.read(receipt(status="Open", is_paid=False))["financial_status"] == "pending"
    assert etsy.read(receipt(status="Canceled"))["cancelled_at"] is not None


@pytest.mark.django_db
def test_an_etsy_sale_records_the_tax_as_paid_by_etsy(
    module_on,  # noqa: F811
    tacoma,  # noqa: F811
    coaster,  # noqa: F811
    django_capture_on_commit_callbacks,
):
    from trammel.notifications.models import SmtpSettings

    smtp = SmtpSettings.load()
    smtp.host, smtp.from_address = "smtp.example.com", "shop@example.com"
    smtp.save()
    with django_capture_on_commit_callbacks(execute=True):
        result = bring_in(receipt(1), receipt(2, total_tax_cost=cents(900), grandtotal=cents(9400)))
    assert result.created == 2
    first = ImportedOrder.objects.get(order_number="3000000001").invoice
    assert first.status == "paid" and first.total == D("93.84") and first.tax_total == D("8.84")
    assert first.tax_remitted_by == "Etsy" and first.tax_location_code == "2717"
    assert not first.tax_flagged
    assert first.lines.first().product == coaster
    assert first.allocations.get().payment.method == Payment.Method.ONLINE
    # Etsy's figure differs from Trammel's rate: noted, not flagged, since it isn't owed.
    second = ImportedOrder.objects.get(order_number="3000000002").invoice
    assert second.tax_total == D("9.00") and not second.tax_flagged and "9.00" in second.tax_note
    # The same Etsy buyer is the same customer, without any email to match on.
    assert first.customer == second.customer and Invoice.objects.count() == 2
    # No email to an Etsy buyer.
    assert not EmailLog.objects.exists() and not mail.outbox
    report = tax_report(dt.date(2026, 11, 1), dt.date(2026, 11, 30))
    row = report["rows"][0]
    assert row["tax"] == D("0") and row["marketplace"] == D("17.84")
    assert report["totals"]["tax"] == D("0") and report["components"] == []
    assert report["marketplaces"] == [{"name": "Etsy", "gross": D("170.00"), "paid": D("187.84")}]


@pytest.mark.django_db
def test_the_tax_report_page_shows_marketplace_sales(module_on, tacoma, admin_client):  # noqa: F811
    bring_in(receipt(1))
    page = admin_client.get("/reports/tax/?start=2026-11-01&end=2026-11-30").content.decode()
    assert "Paid by a marketplace" in page and "Sales through a marketplace facilitator" in page
    csv = admin_client.get("/reports/tax/?start=2026-11-01&end=2026-11-30&format=csv")
    body = b"".join(csv.streaming_content)
    assert b"paid_by_marketplace" in body and b"Marketplace facilitator: Etsy" in body
    sale = ImportedOrder.objects.get().invoice
    assert (
        "collected and paid by Etsy" in admin_client.get(sale.get_absolute_url()).content.decode()
    )
    details = admin_client.get(sale.get_absolute_url() + "?tab=details").content.decode()
    assert "Filed and paid by" in details


@pytest.mark.django_db
def test_canceled_and_refunded_receipts(module_on, tacoma):  # noqa: F811
    result = bring_in(receipt(1, status="Canceled"), receipt(2))
    assert (result.skipped, result.created) == (1, 1)
    bring_in(receipt(2, status="Partially Refunded"))
    row = ImportedOrder.objects.get()
    assert "Partly refunded in Etsy" in row.problem


@pytest.mark.django_db
def test_connecting_etsy_with_pkce_and_the_callback(module_on, admin_client, monkeypatch):  # noqa: F811
    admin_client.post(
        "/imports/settings/etsy/",
        {"etsy-enabled": "on", "etsy-keystring": "keystr1ng", "etsy-shared_secret": "sh4red"},
    )
    assert secrets.get(etsy.SHARED_SECRET_KEY) == "sh4red"
    response = admin_client.post("/imports/settings/etsy/connect/")
    url = urllib.parse.urlparse(response["Location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == etsy.AUTHORIZE_URL
    query = dict(urllib.parse.parse_qsl(url.query))
    pending = json.loads(secrets.get(etsy.PENDING_KEY))
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert query["code_challenge"] == challenge and query["code_challenge_method"] == "S256"
    assert query["client_id"] == "keystr1ng" and query["state"] == pending["state"]
    assert query["redirect_uri"].endswith("/imports/etsy/callback/")
    assert query["scope"] == "transactions_r listings_r shops_r"

    calls = fake_etsy(
        monkeypatch,
        [
            (
                "oauth/token",
                {"access_token": "7001.abc", "refresh_token": "7001.def", "expires_in": 3600},
            ),
            ("users/me", {"user_id": 7001, "shop_id": 555}),
            ("shops/555", {"shop_id": 555, "shop_name": "Eccentric Workshop"}),
        ],
    )
    # A callback that doesn't carry the right state is refused.
    page = admin_client.get(
        "/imports/etsy/callback/", {"code": "c0de", "state": "forged"}, follow=True
    ).content.decode()
    assert "doesn&#x27;t match" in page and not calls
    admin_client.get("/imports/etsy/callback/", {"code": "c0de", "state": query["state"]})
    token_call = calls[0]
    assert token_call["data"]["code_verifier"] == pending["verifier"]
    assert token_call["data"]["redirect_uri"] == query["redirect_uri"]
    assert calls[1]["headers"]["x-api-key"] == "keystr1ng:sh4red"
    assert calls[1]["headers"]["Authorization"] == "Bearer 7001.abc"
    config = EtsySettings.load()
    assert config.is_connected and config.shop_id == "555"
    assert config.shop_name == "Eccentric Workshop"
    assert secrets.get(etsy.REFRESH_TOKEN_KEY) == "7001.def"
    assert not secrets.is_set(etsy.PENDING_KEY)
    page = admin_client.get("/imports/settings/?tab=etsy").content.decode()
    assert "Connected to <strong>Eccentric Workshop</strong>" in page
    assert "7001.abc" not in page and "sh4red" not in page


@pytest.mark.django_db
def test_pasting_the_address_etsy_sent_you_to(module_on, admin_client, monkeypatch):  # noqa: F811
    config = EtsySettings.load()
    config.enabled, config.keystring = True, "keystr1ng"
    config.save()
    secrets.put(etsy.SHARED_SECRET_KEY, "sh4red")
    etsy.begin(config, "https://trammel.lan/imports/etsy/callback/")
    state = json.loads(secrets.get(etsy.PENDING_KEY))["state"]
    calls = fake_etsy(
        monkeypatch,
        [
            ("oauth/token", {"access_token": "7001.abc", "refresh_token": "7001.def"}),
            ("users/me", {"shop_id": 555}),
            ("shops/555", {"shop_name": "Eccentric Workshop"}),
        ],
    )
    pasted = f"https://trammel.lan/imports/etsy/callback/?code=c0de&state={state}"
    admin_client.post("/imports/settings/etsy/paste/", {"code": pasted})
    assert calls[0]["data"]["code"] == "c0de"
    assert calls[0]["data"]["redirect_uri"] == "https://trammel.lan/imports/etsy/callback/"
    assert EtsySettings.load().is_connected
    assert etsy.code_from("  c0de ") == ("c0de", None)


@pytest.mark.django_db
def test_tokens_renew_and_an_expired_connection_asks_to_reconnect(connected, monkeypatch):
    EtsySettings.objects.update(access_token_expires_at=timezone.now())
    calls = fake_etsy(
        monkeypatch,
        [
            (
                "oauth/token",
                {"access_token": "7001.new", "refresh_token": "7001.r2", "expires_in": 3600},
            )
        ],
    )
    assert etsy.access_token(EtsySettings.load()) == "7001.new"
    assert calls[0]["data"]["grant_type"] == "refresh_token"
    assert calls[0]["data"]["refresh_token"] == "7001.refresh"
    assert secrets.get(etsy.REFRESH_TOKEN_KEY) == "7001.r2"
    # Etsy no longer accepts the refresh token.
    EtsySettings.objects.update(access_token_expires_at=timezone.now())
    fake_etsy(
        monkeypatch,
        [("oauth/token", http.HttpProblem(400, "Invalid refresh token", "invalid_grant"))],
    )
    with pytest.raises(etsy.EtsyError, match="Connect Etsy again"):
        etsy.access_token(EtsySettings.load())
    # Ninety days without use.
    EtsySettings.objects.update(refresh_token_expires_at=timezone.now() - dt.timedelta(days=1))
    config = EtsySettings.load()
    assert config.has_expired and not config.is_connected
    with pytest.raises(etsy.EtsyError, match="expired"):
        etsy.access_token(config)


@pytest.mark.django_db
def test_fetching_pages_through_receipts(connected, monkeypatch):
    pages = [{"results": [receipt(n) for n in range(100)]}, {"results": [receipt(100)]}]
    calls = fake_etsy(monkeypatch, [("shops/555/receipts", lambda: pages.pop(0))])
    found = etsy.fetch(EtsySettings.load(), dt.date(2026, 11, 1), dt.date(2026, 11, 7))
    assert len(found) == 101 and len(calls) == 2
    second = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(calls[1]["url"]).query))
    assert second["offset"] == "100" and second["limit"] == "100"
    assert int(second["max_created"]) - int(second["min_created"]) == 7 * 86400 - 1
    assert calls[0]["headers"]["x-api-key"] == "keystr1ng:sh4red"


@pytest.mark.django_db
def test_fetching_etsy_from_the_page(connected, tacoma, admin_client, monkeypatch):  # noqa: F811
    fake_etsy(monkeypatch, [("receipts", {"results": [receipt(1), receipt(2)]})])
    page = admin_client.get("/imports/").content.decode()
    assert "Fetch Etsy orders" in page and "Fetch Shopify orders" not in page
    response = admin_client.post(
        "/imports/fetch/",
        {"source": "etsy", "start": "2026-11-01", "end": "2026-11-07"},
        follow=True,
    )
    assert "Etsy: 2 new sales" in response.content.decode()
    assert ImportedOrder.objects.filter(source="etsy").count() == 2


@pytest.mark.django_db
def test_changing_the_app_disconnects_and_nothing_goes_out_offline(connected, admin_client):
    from trammel.core.models import SystemSettings

    system = SystemSettings.load()
    system.offline_mode = True
    system.save()
    with pytest.raises(etsy.EtsyError, match="Outbound traffic"):
        etsy.fetch(EtsySettings.load(), dt.date(2026, 11, 1), dt.date(2026, 11, 1))
    admin_client.post(
        "/imports/settings/etsy/", {"etsy-enabled": "on", "etsy-keystring": "an0ther"}
    )
    config = EtsySettings.load()
    assert config.keystring == "an0ther" and not config.is_connected
    assert not secrets.is_set(etsy.REFRESH_TOKEN_KEY)
