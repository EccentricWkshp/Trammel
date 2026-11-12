"""Shippo: fetching orders and reading them (ported from sales_tracker).

Shippo's live API token (Settings, Advanced, API) signs every request
("ShippoToken ..."). Orders come from /orders by the date they were placed,
25 to a page, following Shippo's next-page links (only ever to Shippo itself).
Orders still awaiting payment are left out.

Shippo gathers orders from other stores and marketplaces; each says which
(shop_app). One from a store Trammel imports itself only adds what that
order is missing (origins.py).
"""

from __future__ import annotations

import datetime as dt
import urllib.parse
from decimal import Decimal

from django.utils import timezone

from trammel.core import secrets

from . import origins
from .http import HttpProblem, StoreError, call
from .models import ShippoSettings

TOKEN_KEY = "imports.shippo.token"
LABEL = "Shippo"
BASE = "https://api.goshippo.com"
#: A runaway guard: 400 pages of 25 is 10,000 orders in one fetch.
MAX_PAGES = 400

PAID = {"PAID", "SHIPPED", "PARTIALLY_FULFILLED"}


class ShippoError(StoreError):
    """Something went wrong talking to Shippo; the message says what to do."""


def _headers() -> dict:
    token = secrets.get(TOKEN_KEY)
    if not token:
        raise ShippoError("Enter the API token in Settings, Online stores.")
    return {"Authorization": f"ShippoToken {token}", "Accept": "application/json"}


def _get(url: str):
    if not url.startswith(f"{BASE}/"):
        raise ShippoError("Shippo's next-page link pointed somewhere else; stopped.")
    try:
        payload, _ = call(LABEL, "GET", url, headers=_headers())
    except HttpProblem as exc:
        if exc.status == 401:
            raise ShippoError(
                "Shippo refused the token (401). Check the API token in Settings, Online "
                "stores: the live token, not the test one."
            ) from None
        raise ShippoError(f"Shippo answered {exc.status}: {exc.detail}") from None
    return payload


def fetch(config: ShippoSettings, start: dt.date, end: dt.date) -> list[dict]:
    """Every order placed from `start` through `end`, raw."""
    query = urllib.parse.urlencode(
        {
            "results": 25,
            "page": 1,
            "start_date": f"{start.isoformat()}T00:00:00",
            "end_date": f"{end.isoformat()}T23:59:59",
        }
    )
    url = f"{BASE}/orders/?{query}"
    orders: list[dict] = []
    for _ in range(MAX_PAGES):
        payload = _get(url)
        orders.extend(payload.get("results") or [])
        url = payload.get("next")
        if not url:
            break
    return orders


def _money(value) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except ArithmeticError:
        return Decimal("0")


def _when(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if timezone.is_aware(moment) else moment.replace(tzinfo=dt.UTC)


def _label(order: dict) -> dict:
    """The newest label bought for the order: its tracking number, carrier and when."""
    found = [t for t in order.get("transactions") or [] if isinstance(t, dict)]
    found = [t for t in found if (t.get("status") or "SUCCESS") == "SUCCESS"]
    if not found:
        return {}
    last = max(found, key=lambda t: t.get("object_created") or "")
    rate = last.get("rate") if isinstance(last.get("rate"), dict) else {}
    when = _when(last.get("object_created"))
    return {
        "tracking": last.get("tracking_number") or "",
        "carrier": (rate.get("provider") or last.get("carrier") or "").upper()[:60],
        "shipped_on": timezone.localtime(when).date() if when else None,
    }


def read(order: dict) -> dict:
    """A Shippo order as the importer reads every store's."""
    to = order.get("to_address") or {}
    customer = {
        "name": to.get("name") or "",
        "email": (to.get("email") or order.get("email") or "").strip(),
        "company": to.get("company") or "",
        "phone": to.get("phone") or "",
        "line1": to.get("street1") or "",
        "line2": " ".join(filter(None, [to.get("street2"), to.get("street3")])),
        "city": to.get("city") or "",
        "region": to.get("state") or "",
        "postal_code": to.get("zip") or "",
        "country": (to.get("country") or "")[:2],
    }
    status = (order.get("order_status") or "").upper()
    label = _label(order)
    # An order made in Shippo itself may never be marked paid; once a label is
    # bought for it, it has been sold.
    paid = status in PAID or (status in ("", "UNKNOWN") and bool(label))
    app = order.get("shop_app") or ""
    lines = []
    for item in order.get("line_items") or []:
        quantity = int(item.get("quantity") or 0)
        if quantity <= 0:
            continue
        unit = (_money(item.get("total_price")) / quantity).quantize(Decimal("0.01"))
        lines.append(
            {
                "sku": (item.get("sku") or "").strip(),
                "title": item.get("title") or "Item",
                "variant": item.get("variant_title") or "",
                "quantity": str(quantity),
                "unit_price": str(unit),
            }
        )
    return {
        "external_order_id": str(order.get("object_id") or ""),
        "order_number": str(order.get("order_number") or order.get("object_id") or ""),
        "order_date": _when(order.get("placed_at") or order.get("object_created")),
        "buyer_id": "",
        "test": False,
        "unpaid": not paid and status not in ("CANCELLED", "REFUNDED"),
        "cancelled_at": _when(order.get("object_updated")) or timezone.now()
        if status == "CANCELLED"
        else None,
        "financial_status": "refunded" if status == "REFUNDED" else "paid" if paid else "pending",
        "customer": customer,
        "customer_unavailable": not any(
            [customer["email"], customer["name"], customer["line1"], customer["postal_code"]]
        ),
        "lines": lines,
        "discount": "0",
        "shipping": str(_money(order.get("shipping_cost"))),
        "tax": str(_money(order.get("total_tax"))),
        "total": str(_money(order.get("total_price"))),
        "taxes_included": False,
        "outstanding": "0",
        "carrier": label.get("carrier", ""),
        "tracking": label.get("tracking", ""),
        "shipped_on": label.get("shipped_on"),
        "notes": order.get("notes") or "",
        "origin": origins.store(app),
        "origin_label": app,
        "tax_remitted_by": origins.remitted_by(app),
    }
