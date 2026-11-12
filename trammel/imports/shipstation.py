"""ShipStation: fetching orders and their shipments, and reading them (ported
from sales_tracker).

ShipStation's API key and secret (Settings, Account, API Settings) sign every
request (HTTP Basic). Orders come from /orders by order date, without those
still awaiting payment; shipments come from /shipments, once for the whole
fetch rather than per order, since ShipStation allows only 40 calls a minute.

ShipStation gathers orders from other stores. Each says where it came from
(advancedOptions.source): one from a store Trammel imports itself only adds
what that order is missing (origins.py).

ShipStation writes its dates and times in US Pacific time, without an offset.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone

from trammel.core import secrets

from . import origins
from .http import HttpProblem, StoreError, call
from .models import ShipStationSettings

KEY_KEY = "imports.shipstation.api_key"
SECRET_KEY = "imports.shipstation.api_secret"
LABEL = "ShipStation"
BASE = "https://ssapi.shipstation.com"
PACIFIC = ZoneInfo("America/Los_Angeles")
#: A runaway guard: 40 pages of 500 is 20,000 orders in one fetch.
MAX_PAGES = 40

PAID = {"awaiting_shipment", "pending_fulfillment", "shipped", "on_hold"}


class ShipStationError(StoreError):
    """Something went wrong talking to ShipStation; the message says what to do."""


def _headers() -> dict:
    import base64

    key, secret = secrets.get(KEY_KEY), secrets.get(SECRET_KEY)
    if not (key and secret):
        raise ShipStationError("Enter the API key and secret in Settings, Online stores.")
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def _get(path: str, params: dict):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    try:
        payload, _ = call(LABEL, "GET", url, headers=_headers())
    except HttpProblem as exc:
        if exc.status == 401:
            raise ShipStationError(
                "ShipStation refused the key (401). Check the API key and secret in Settings, "
                "Online stores."
            ) from None
        if exc.status == 429:
            raise ShipStationError(
                "ShipStation allows 40 requests a minute; wait a minute and fetch again."
            ) from None
        raise ShipStationError(f"ShipStation answered {exc.status}: {exc.detail}") from None
    return payload


def _pages(path: str, params: dict, key: str) -> list[dict]:
    found: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        payload = _get(path, {**params, "pageSize": 500, "page": page})
        found.extend(payload.get(key) or [])
        if page >= int(payload.get("pages") or 1):
            break
    return found


def fetch(config: ShipStationSettings, start: dt.date, end: dt.date) -> list[dict]:
    """Every order from `start` through `end`, each with its latest shipment."""
    orders = _pages(
        "/orders",
        {
            "orderDateStart": f"{start.isoformat()} 00:00:00",
            "orderDateEnd": f"{end.isoformat()} 23:59:59",
        },
        "orders",
    )
    orders = [o for o in orders if o.get("orderStatus") != "awaiting_payment"]
    if any(o.get("orderStatus") == "shipped" for o in orders):
        shipments = _pages(
            "/shipments",
            {
                "shipDateStart": start.isoformat(),
                "shipDateEnd": (timezone.localdate() + dt.timedelta(days=1)).isoformat(),
            },
            "shipments",
        )
        latest: dict = {}
        for shipment in shipments:
            if shipment.get("voided"):
                continue
            latest[shipment.get("orderId")] = shipment
        for order in orders:
            if order.get("orderId") in latest:
                order["_shipment"] = latest[order["orderId"]]
    return orders


def _money(value) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except ArithmeticError:
        return Decimal("0")


def _when(value) -> dt.datetime | None:
    """ShipStation's Pacific-time stamp, "2026-11-11T08:46:27.0000000", as a moment."""
    if not value:
        return None
    text = str(value).split(".")[0]
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if timezone.is_aware(moment) else moment.replace(tzinfo=PACIFIC)


def _day(value) -> dt.date | None:
    moment = _when(value)
    return moment.date() if moment else None


def read(order: dict) -> dict:
    """A ShipStation order as the importer reads every store's."""
    ship = order.get("shipTo") or {}
    bill = order.get("billTo") or {}
    customer = {
        "name": ship.get("name") or bill.get("name") or "",
        "email": (order.get("customerEmail") or "").strip(),
        "company": ship.get("company") or bill.get("company") or "",
        "phone": ship.get("phone") or bill.get("phone") or "",
        "line1": ship.get("street1") or "",
        "line2": " ".join(filter(None, [ship.get("street2"), ship.get("street3")])),
        "city": ship.get("city") or "",
        "region": ship.get("state") or "",
        "postal_code": ship.get("postalCode") or "",
        "country": (ship.get("country") or "")[:2],
    }
    status = order.get("orderStatus") or ""
    advanced = order.get("advancedOptions") or {}
    source = advanced.get("source") or ""
    shipment = order.get("_shipment") or {}
    carrier = (shipment.get("carrierCode") or order.get("carrierCode") or "").replace("_", " ")
    lines = [
        {
            "sku": (item.get("sku") or "").strip(),
            "title": item.get("name") or "Item",
            "variant": "",
            "quantity": str(int(item.get("quantity") or 0)),
            "unit_price": str(_money(item.get("unitPrice"))),
        }
        for item in order.get("items") or []
        if int(item.get("quantity") or 0) > 0
    ]
    return {
        "external_order_id": str(order.get("orderId") or ""),
        "order_number": str(order.get("orderNumber") or order.get("orderId") or ""),
        "order_date": _when(order.get("orderDate") or order.get("createDate")),
        "buyer_id": str(order.get("customerId") or "") if order.get("customerId") else "",
        "test": False,
        "unpaid": status == "awaiting_payment",
        "cancelled_at": _when(order.get("modifyDate")) or timezone.now()
        if status == "cancelled"
        else None,
        "financial_status": "paid" if status in PAID else "pending",
        "customer": customer,
        "customer_unavailable": not any(
            [customer["email"], customer["name"], customer["line1"], customer["postal_code"]]
        ),
        "lines": lines,
        "discount": "0",
        "shipping": str(_money(order.get("shippingAmount"))),
        "tax": str(_money(order.get("taxAmount"))),
        "total": str(_money(order.get("orderTotal"))),
        "taxes_included": False,
        "outstanding": "0",
        "carrier": carrier.upper()[:60],
        "tracking": shipment.get("trackingNumber") or "",
        "shipped_on": _day(shipment.get("shipDate") or order.get("shipDate")),
        "notes": order.get("customerNotes") or "",
        # Where ShipStation got the order: a store Trammel imports itself, or not.
        "origin": origins.store(source),
        "origin_label": source,
        "tax_remitted_by": origins.remitted_by(source),
    }
