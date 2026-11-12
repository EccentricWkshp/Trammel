"""WooCommerce, ShipStation and Shippo (0.7.3): reading their orders, fetching
them, and orders a gathering service passes on from a store Trammel imports
itself, which only add what that store's own order is missing."""

import datetime as dt
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from tests.test_imports import module_on, order, tacoma  # noqa: F401
from trammel.billing.models import Invoice
from trammel.core import secrets
from trammel.imports import origins, services, shippo, shipstation, shopify, woocommerce
from trammel.imports.models import (
    ImportedOrder,
    ShippoSettings,
    ShipStationSettings,
    ShopifySettings,
    WooCommerceSettings,
)

D = Decimal


def woo_order(n=501, **over) -> dict:
    base = {
        "id": 9000 + n,
        "number": str(n),
        "status": "processing",
        "date_created_gmt": "2026-11-02T18:15:00",
        "date_modified_gmt": "2026-11-03T18:15:00",
        "customer_id": 42,
        "customer_note": "Leave at the door.",
        "prices_include_tax": False,
        "discount_total": "5.00",
        "shipping_total": "10.00",
        "total_tax": "8.84",
        "total": "93.84",
        "billing": {
            "first_name": "Grace",
            "last_name": "Hopper",
            "email": "grace@example.com",
            "phone": "555-0100",
            "address_1": "1 Navy Way",
            "city": "Tacoma",
            "state": "WA",
            "postcode": "98402",
            "country": "US",
        },
        "shipping": {
            "first_name": "Grace",
            "last_name": "Hopper",
            "address_1": "1 Navy Way",
            "city": "Tacoma",
            "state": "WA",
            "postcode": "98402",
            "country": "US",
        },
        "line_items": [
            {"name": "Walnut coaster", "sku": "CST-WAL", "quantity": 2, "subtotal": "80.00"}
        ],
        "fee_lines": [],
        "meta_data": [
            {
                "key": "_wc_shipment_tracking_items",
                "value": [
                    {
                        "tracking_provider": "usps",
                        "tracking_number": "9400",
                        "date_shipped": "1762300800",
                    }
                ],
            }
        ],
    }
    base.update(over)
    return base


def ss_order(n=1001, **over) -> dict:
    base = {
        "orderId": 7000 + n,
        "orderNumber": str(n),
        "orderDate": "2026-11-02T10:15:00.0000000",
        "modifyDate": "2026-11-03T10:15:00.0000000",
        "orderStatus": "shipped",
        "customerId": 88,
        "customerEmail": "ada@example.com",
        "shipTo": {
            "name": "Ada Lovelace",
            "street1": "2 Engine St",
            "city": "Tacoma",
            "state": "WA",
            "postalCode": "98402",
            "country": "US",
        },
        "items": [{"name": "Maple board", "sku": "CB-MAP", "quantity": 1, "unitPrice": 85.0}],
        "orderTotal": 103.85,
        "taxAmount": 8.85,
        "shippingAmount": 10.0,
        "customerNotes": "",
        "advancedOptions": {"source": "amazon", "storeId": 1},
    }
    base.update(over)
    return base


def shippo_order(n="SH-9", **over) -> dict:
    base = {
        "object_id": f"obj-{n}",
        "order_number": n,
        "order_status": "PAID",
        "placed_at": "2026-11-02T18:15:00Z",
        "shop_app": "Shippo",
        "to_address": {
            "name": "Alan Turing",
            "email": "alan@example.com",
            "street1": "3 Bletchley Rd",
            "city": "Tacoma",
            "state": "WA",
            "zip": "98402",
            "country": "US",
        },
        "line_items": [
            {"title": "Coaster set", "sku": "CO-SET", "quantity": 2, "total_price": "72.00"}
        ],
        "shipping_cost": "8.00",
        "total_tax": "7.34",
        "total_price": "87.34",
        "transactions": [
            {
                "status": "SUCCESS",
                "tracking_number": "92055",
                "object_created": "2026-11-03T17:00:00Z",
                "rate": {"provider": "usps"},
            }
        ],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- reading


def test_a_woocommerce_order_reads_like_any_store():
    read = woocommerce.read(woo_order())
    assert read["order_number"] == "501" and read["financial_status"] == "paid"
    assert read["customer"]["name"] == "Grace Hopper" and read["customer"]["region"] == "WA"
    assert read["lines"][0]["unit_price"] == "40.00"  # before the coupon; the discount is a line
    assert (read["discount"], read["tax"], read["total"]) == ("5.00", "8.84", "93.84")
    assert (read["carrier"], read["tracking"]) == ("USPS", "9400")
    assert read["shipped_on"] == dt.date(2025, 11, 5)
    assert read["order_date"].tzinfo is not None
    assert woocommerce.read(woo_order(status="on-hold"))["unpaid"]
    assert woocommerce.read(woo_order(status="cancelled"))["cancelled_at"] is not None
    assert woocommerce.read(woo_order(status="refunded"))["financial_status"] == "refunded"


def test_the_woocommerce_address_must_be_public_https():
    assert woocommerce.check_url("shop.example.com/") == "https://shop.example.com"
    for bad in ("http://shop.example.com", "https://localhost", "https://192.168.1.5"):
        with pytest.raises(ValidationError):
            woocommerce.check_url(bad)


def test_a_shipstation_order_reads_in_pacific_time_with_its_shipment():
    raw = ss_order()
    raw["_shipment"] = {"trackingNumber": "1Z1", "carrierCode": "ups", "shipDate": "2026-11-03"}
    read = shipstation.read(raw)
    assert read["order_date"].utcoffset() == dt.timedelta(hours=-8)  # PST in November
    assert (read["carrier"], read["tracking"], read["shipped_on"]) == (
        "UPS",
        "1Z1",
        dt.date(2026, 11, 3),
    )
    # From Amazon: Amazon pays the sales tax; Trammel doesn't import Amazon itself.
    assert (read["origin"], read["tax_remitted_by"]) == ("", "Amazon")
    assert shipstation.read(ss_order(advancedOptions={"source": "shopify"}))["origin"] == (
        "shopify"
    )


def test_a_shippo_order_reads_with_its_label():
    read = shippo.read(shippo_order())
    assert read["lines"][0]["unit_price"] == "36.00"
    assert (read["carrier"], read["tracking"]) == ("USPS", "92055")
    assert not read["unpaid"] and read["financial_status"] == "paid"
    # Made in Shippo and never marked paid: a sale once a label is bought.
    assert not shippo.read(shippo_order(order_status="UNKNOWN"))["unpaid"]
    assert shippo.read(shippo_order(order_status="UNKNOWN", transactions=[]))["unpaid"]
    assert shippo.read(shippo_order(order_status="AWAITING_PAYMENT"))["unpaid"]


def test_origins_are_recognized():
    assert origins.store("Shopify") == "shopify" and origins.store("WooCommerce") == "woocommerce"
    assert origins.store("Amazon Seller Central") == ""
    assert origins.remitted_by("Amazon Seller Central") == "Amazon"
    assert origins.remitted_by("Etsy") == "Etsy" and origins.remitted_by("shopify") == ""
    assert origins.number("#1001") == origins.number(" 1001 ") == "1001"


# ---------------------------------------------------------------- fetching


@pytest.mark.django_db
def test_woocommerce_fetches_every_page_with_the_key(module_on, monkeypatch):  # noqa: F811
    config = WooCommerceSettings.load()
    config.enabled, config.store_url = True, "https://shop.example.com"
    config.save()
    secrets.put(woocommerce.KEY_KEY, "ck_1")
    secrets.put(woocommerce.SECRET_KEY, "cs_1")
    calls = []

    def fake(label, method, url, *, data=None, headers=None):
        calls.append((url, headers))
        return [woo_order(len(calls))], {"X-WP-TotalPages": "2"}

    monkeypatch.setattr(woocommerce, "call", fake)
    found = woocommerce.fetch(config, dt.date(2026, 11, 1), dt.date(2026, 11, 7))
    assert len(found) == 2 and len(calls) == 2
    assert calls[0][0].startswith("https://shop.example.com/wp-json/wc/v3/orders?")
    assert "after=2026-11-01T00%3A00%3A00" in calls[0][0] and "page=2" in calls[1][0]
    assert calls[0][1]["Authorization"].startswith("Basic ")


@pytest.mark.django_db
def test_shipstation_fetches_orders_then_shipments_once(module_on, monkeypatch):  # noqa: F811
    secrets.put(shipstation.KEY_KEY, "k")
    secrets.put(shipstation.SECRET_KEY, "s")
    calls = []

    def fake(label, method, url, *, data=None, headers=None):
        calls.append(url)
        if "/orders?" in url:
            return {
                "orders": [ss_order(1), ss_order(2, orderStatus="awaiting_payment")],
                "pages": 1,
            }, {}
        return {"shipments": [{"orderId": 7001, "trackingNumber": "1Z1"}], "pages": 1}, {}

    monkeypatch.setattr(shipstation, "call", fake)
    found = shipstation.fetch(
        ShipStationSettings.load(), dt.date(2026, 11, 1), dt.date(2026, 11, 7)
    )
    assert [o["orderId"] for o in found] == [7001]  # not the one awaiting payment
    assert found[0]["_shipment"]["trackingNumber"] == "1Z1"
    assert len(calls) == 2 and all(c.startswith("https://ssapi.shipstation.com/") for c in calls)


@pytest.mark.django_db
def test_shippo_follows_only_its_own_next_links(module_on, monkeypatch):  # noqa: F811
    secrets.put(shippo.TOKEN_KEY, "shippo_live_1")
    pages = [
        ({"results": [shippo_order("1")], "next": "https://api.goshippo.com/orders/?page=2"}, {}),
        ({"results": [shippo_order("2")], "next": "https://evil.example.com/steal"}, {}),
    ]
    monkeypatch.setattr(shippo, "call", lambda *a, **k: pages.pop(0))
    with pytest.raises(shippo.ShippoError, match="somewhere else"):
        shippo.fetch(ShippoSettings.load(), dt.date(2026, 11, 1), dt.date(2026, 11, 7))


# ------------------------------------------------- orders from other stores


def _shopify_connected():
    config = ShopifySettings.load()
    config.enabled, config.shop_domain = True, "eccentric.myshopify.com"
    config.save()


@pytest.mark.django_db
def test_an_order_from_a_store_imported_directly_only_adds_what_it_lacks(
    module_on,  # noqa: F811
    tacoma,  # noqa: F811
):
    own = order(1001, fulfillments=[])
    services.import_orders([shopify.read(own)], "shopify")
    row = ImportedOrder.objects.get(source="shopify")
    assert (row.tracking, row.shipped_on) == ("", None)
    sales = Invoice.objects.count()

    relayed = ss_order(1001, advancedOptions={"source": "shopify"})
    relayed["_shipment"] = {"trackingNumber": "1Z9", "carrierCode": "ups", "shipDate": "2026-11-04"}
    result = services.import_orders(
        [shipstation.read(relayed)], "shipstation", direct=frozenset({"shopify"})
    )
    assert result.merged == 1 and result.created == 0
    row.refresh_from_db()
    assert (row.carrier, row.tracking, row.shipped_on) == ("UPS", "1Z9", dt.date(2026, 11, 4))
    assert Invoice.objects.count() == sales  # no second sale
    assert not ImportedOrder.objects.filter(source="shipstation").exists()
    # What the store said is kept.
    relayed["_shipment"]["trackingNumber"] = "OTHER"
    services.import_orders(
        [shipstation.read(relayed)], "shipstation", direct=frozenset({"shopify"})
    )
    row.refresh_from_db()
    assert row.tracking == "1Z9"


@pytest.mark.django_db
def test_a_waiting_order_gets_its_buyer_from_shipstation(module_on, tacoma):  # noqa: F811
    hidden = order(1002, email="", customer={}, shipping_address={"province_code": "WA"})
    services.import_orders([shopify.read(hidden)], "shopify")
    row = ImportedOrder.objects.get(source="shopify")
    assert row.status == "pending"
    relayed = ss_order(1002, advancedOptions={"source": "shopify"})
    services.import_orders(
        [shipstation.read(relayed)], "shipstation", direct=frozenset({"shopify"})
    )
    row.refresh_from_db()
    assert row.status == "imported" and row.customer.name == "Ada Lovelace"
    assert row.invoice.total == D("93.84")  # Shopify's own figures, not ShipStation's


@pytest.mark.django_db
def test_orders_from_elsewhere_become_sales_and_unfetched_ones_wait(
    module_on,  # noqa: F811
    tacoma,  # noqa: F811
):
    result = services.import_orders(
        [
            shipstation.read(ss_order(1)),  # from Amazon: not a store Trammel imports
            shipstation.read(ss_order(2, advancedOptions={"source": "shopify"})),
            shipstation.read(ss_order(3, orderStatus="awaiting_payment")),
        ],
        "shipstation",
        direct=frozenset({"shopify"}),
    )
    assert (result.created, result.elsewhere, result.skipped) == (1, 1, 1)
    sale = ImportedOrder.objects.get(source="shipstation").invoice
    assert "from amazon" in sale.internal_notes
    assert "fetch those stores, then this again" in result.summary("ShipStation")


@pytest.mark.django_db
def test_without_the_store_connected_its_orders_come_in(module_on, tacoma):  # noqa: F811
    result = services.import_orders(
        [shipstation.read(ss_order(5, advancedOptions={"source": "shopify"}))], "shipstation"
    )
    assert result.created == 1


# ---------------------------------------------------------------- the pages


@pytest.mark.django_db
def test_connecting_and_fetching_from_the_pages(
    module_on,  # noqa: F811
    tacoma,  # noqa: F811
    admin_client,
    monkeypatch,
):
    page = admin_client.get("/imports/settings/?tab=shipstation").content.decode()
    assert "Settings, Account, API Settings" in page
    response = admin_client.post(
        "/imports/settings/store/shipstation/", {"shipstation-enabled": "on"}
    )
    assert "Enter this to connect." in response.content.decode()
    admin_client.post(
        "/imports/settings/store/shipstation/",
        {
            "shipstation-enabled": "on",
            "shipstation-api_key": "ss-key-ABC",
            "shipstation-api_secret": "s1",
        },
    )
    assert secrets.get(shipstation.KEY_KEY) == "ss-key-ABC"
    page = admin_client.get("/imports/settings/?tab=shipstation").content.decode()
    assert "Saved; leave blank to keep it." in page and "ss-key-ABC" not in page

    _shopify_connected()
    own = order(1001, fulfillments=[])
    services.import_orders([shopify.read(own)], "shopify")

    def fake_fetch(cfg, start, end):
        relayed = ss_order(1001, advancedOptions={"source": "shopify"})
        relayed["_shipment"] = {"trackingNumber": "1Z9", "carrierCode": "ups"}
        return [relayed, ss_order(2)]

    monkeypatch.setattr(shipstation, "fetch", fake_fetch)
    response = admin_client.post(
        "/imports/fetch/",
        {"source": "shipstation", "start": "2026-11-01", "end": "2026-11-07"},
        follow=True,
    )
    text = response.content.decode()
    assert "ShipStation: 1 new sale" in text and "shipping details added" in text
    assert ImportedOrder.objects.get(source="shopify").tracking == "1Z9"

    monkeypatch.setattr(shipstation, "fetch", lambda *a: [])
    response = admin_client.post("/imports/settings/store/shipstation/test/", follow=True)
    assert "Connected to ShipStation: 0 order(s) today." in response.content.decode()
    assert admin_client.get("/imports/settings/store/nope/test/").status_code == 405
