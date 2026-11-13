"""WooCommerce: fetching orders from the store's REST API and reading them
(the order format as WCtoSS reads it).

The store makes a REST API key (WooCommerce, Settings, Advanced, REST API) with
Read access: a consumer key and secret, sent over HTTPS only. Orders come from
wc/v3/orders by the date they were created. WooCommerce is the seller's own
store, so the seller collects and remits its sales tax.
"""

from __future__ import annotations

import base64
import datetime as dt
import urllib.parse
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils import timezone

from trammel.core import secrets

from .http import HttpProblem, StoreError, call
from .models import WooCommerceSettings

KEY_KEY = "imports.woocommerce.consumer_key"
SECRET_KEY = "imports.woocommerce.consumer_secret"
LABEL = "WooCommerce"
#: A runaway guard: 100 pages of 100 is 10,000 orders in one fetch.
MAX_PAGES = 100

#: WooCommerce's order statuses, as the importer reads payment. Orders not yet
#: paid, failed, or in the bin are left out (see SKIP).
STATUS = {
    "processing": "paid",
    "completed": "paid",
    "refunded": "refunded",
    "cancelled": "cancelled",
}
SKIP = {"pending", "on-hold", "failed", "trash", "checkout-draft"}


class WooCommerceError(StoreError):
    """Something went wrong talking to WooCommerce; the message says what to do."""


def check_url(value: str) -> str:
    """The store's address as https://host[/path], or ValidationError. Only HTTPS,
    and never an address on this machine or the local network."""
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValidationError("Enter the store's address, starting with https://.")
    host = parts.hostname
    # A number in any spelling (2130706433 is 127.0.0.1) is an address, and only
    # a public one will do. A name is resolved and checked each time Trammel
    # connects (core.outbound, SEC-10, 0.10.6).
    from trammel.core.outbound import public_address

    address = public_address(host)
    if host == "localhost" or host.endswith(".localhost") or (address and not address.is_global):
        raise ValidationError("Enter the store's public address, not one on your own network.")
    return f"https://{parts.netloc}{parts.path}".rstrip("/")


def _headers() -> dict:
    key, secret = secrets.get(KEY_KEY), secrets.get(SECRET_KEY)
    if not (key and secret):
        raise WooCommerceError("Enter the consumer key and secret in Settings, Online stores.")
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def _get(url: str):
    try:
        return call(LABEL, "GET", url, headers=_headers())
    except HttpProblem as exc:
        if exc.status == 401:
            raise WooCommerceError(
                "WooCommerce refused the key (401). Check the consumer key and secret, and "
                "that the key has Read access."
            ) from None
        if exc.status == 404:
            raise WooCommerceError(
                "The store has no WooCommerce REST API at that address (404). Check the "
                "store address, and that pretty permalinks are on in WordPress."
            ) from None
        raise WooCommerceError(f"WooCommerce answered {exc.status}: {exc.detail}") from None


def fetch(config: WooCommerceSettings, start: dt.date, end: dt.date) -> list[dict]:
    """Every order created from `start` through `end` (the store's dates), raw."""
    base = check_url(config.store_url)
    if not base:
        raise WooCommerceError("Enter the store's address in Settings, Online stores.")
    orders: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode(
            {
                "after": f"{start.isoformat()}T00:00:00",
                "before": f"{(end + dt.timedelta(days=1)).isoformat()}T00:00:00",
                "per_page": 100,
                "page": page,
                "orderby": "date",
                "order": "asc",
            }
        )
        payload, headers = _get(f"{base}/wp-json/wc/v3/orders?{query}")
        if not isinstance(payload, list):
            raise WooCommerceError("WooCommerce sent back something other than a list of orders.")
        orders.extend(payload)
        if page >= int(headers.get("X-WP-TotalPages") or 1):
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
    # date_created_gmt has no offset but is UTC.
    return moment if timezone.is_aware(moment) else moment.replace(tzinfo=dt.UTC)


def _meta(order: dict, key: str):
    return next((m.get("value") for m in order.get("meta_data") or [] if m.get("key") == key), None)


def _tracking(order: dict) -> tuple[str, str, dt.date | None]:
    """(carrier, tracking number, shipped on) from the Shipment Tracking extension,
    when the store uses it."""
    items = _meta(order, "_wc_shipment_tracking_items") or []
    if not (isinstance(items, list) and items and isinstance(items[-1], dict)):
        return "", "", None
    last = items[-1]
    shipped = None
    stamp = last.get("date_shipped")
    if stamp not in (None, ""):
        try:
            shipped = dt.datetime.fromtimestamp(int(stamp), tz=dt.UTC).date()
        except TypeError, ValueError, OverflowError:
            shipped = None
    carrier = last.get("tracking_provider") or last.get("custom_tracking_provider") or ""
    return (
        str(carrier).replace("-", " ").upper()[:60],
        str(last.get("tracking_number") or ""),
        shipped,
    )


def read(order: dict) -> dict:
    """A WooCommerce order as the importer reads every store's."""
    shipping = order.get("shipping") or {}
    billing = order.get("billing") or {}
    address = shipping if (shipping.get("address_1") or shipping.get("city")) else billing
    name = " ".join(
        filter(None, [address.get("first_name"), address.get("last_name")])
    ) or " ".join(filter(None, [billing.get("first_name"), billing.get("last_name")]))
    customer = {
        "name": name,
        "email": (billing.get("email") or "").strip(),
        "company": address.get("company") or billing.get("company") or "",
        "phone": shipping.get("phone") or billing.get("phone") or "",
        "line1": address.get("address_1") or "",
        "line2": address.get("address_2") or "",
        "city": address.get("city") or "",
        "region": address.get("state") or "",
        "postal_code": address.get("postcode") or "",
        "country": (address.get("country") or "")[:2],
    }
    status = order.get("status") or ""
    carrier, tracking, shipped_on = _tracking(order)
    lines = [
        {
            "sku": (line.get("sku") or "").strip(),
            "title": line.get("name") or "Item",
            "variant": "",
            "quantity": str(int(line.get("quantity") or 0)),
            # The price before the order's coupons; the discount is its own line.
            "unit_price": str(
                (_money(line.get("subtotal")) / int(line.get("quantity") or 1)).quantize(
                    Decimal("0.01")
                )
            ),
        }
        for line in order.get("line_items") or []
        if int(line.get("quantity") or 0) > 0
    ]
    fees = [
        {
            "sku": "",
            "title": fee.get("name") or "Fee",
            "variant": "",
            "quantity": "1",
            "unit_price": str(_money(fee.get("total"))),
        }
        for fee in order.get("fee_lines") or []
        if _money(fee.get("total"))
    ]
    return {
        "external_order_id": str(order.get("id") or ""),
        "order_number": str(order.get("number") or order.get("id") or ""),
        "order_date": _when(order.get("date_created_gmt") or order.get("date_created")),
        "buyer_id": str(order.get("customer_id") or "") if order.get("customer_id") else "",
        "test": False,
        # Not paid yet, failed or binned: not a sale, so not brought in.
        "unpaid": status in SKIP,
        "cancelled_at": _when(order.get("date_modified_gmt")) or timezone.now()
        if status == "cancelled"
        else None,
        "financial_status": STATUS.get(status, "pending"),
        "customer": customer,
        "customer_unavailable": not any(
            [customer["email"], name, customer["line1"], customer["postal_code"]]
        ),
        "lines": lines + fees,
        "discount": str(_money(order.get("discount_total"))),
        "shipping": str(_money(order.get("shipping_total"))),
        "tax": str(_money(order.get("total_tax"))),
        "total": str(_money(order.get("total"))),
        "taxes_included": bool(order.get("prices_include_tax")),
        "outstanding": "0",
        "carrier": carrier,
        "tracking": tracking,
        "shipped_on": shipped_on,
        "notes": order.get("customer_note") or "",
    }
