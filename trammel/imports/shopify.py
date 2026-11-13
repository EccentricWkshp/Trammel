"""Shopify: signing in, fetching orders, and reading them (ported from sales_tracker).

Two ways to sign in, chosen in Settings, Online stores:

* **Admin API access token** (a custom app made in the store's admin): a
  long-lived shpat_ token, pasted once. Its API secret key signs webhooks.
* **Client ID and secret** (an app made in the Dev Dashboard): exchanged for a
  24-hour token, kept until five minutes before it runs out. The client secret
  signs webhooks.

The app needs the read_orders, read_customers, read_fulfillments and
read_products scopes. read_orders reaches back 60 days; older orders need
read_all_orders. Below Shopify's Advanced plan, a custom app made in the
store's admin gets no buyer name, email, phone or address ("protected
customer data"); a Dev Dashboard app does (tested 11/11/2026). Orders
without their buyer wait for a customer in Trammel instead of becoming
half-identified sales.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from trammel.core import secrets

from .models import ShopifySettings

TOKEN_KEY = "imports.shopify.token"  # Admin API access token (custom app)
API_SECRET_KEY = "imports.shopify.api_secret"  # signs webhooks for a custom app
CLIENT_SECRET_KEY = "imports.shopify.client_secret"  # Dev Dashboard app
CACHED_TOKEN_KEY = "imports.shopify.access_token"  # the 24-hour token from the exchange

#: Refresh a little early so a token can't run out partway through an import.
TOKEN_MARGIN = dt.timedelta(minutes=5)
#: A runaway guard: 100 pages of 250 is 25,000 orders in one fetch.
MAX_PAGES = 100
TIMEOUT = 30
_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")


class ShopifyError(Exception):
    """Something went wrong talking to Shopify; the message says what to do."""


def api_version() -> str:
    return getattr(settings, "SHOPIFY_API_VERSION", "2026-01")


def normalize_domain(value: str) -> str:
    """Whatever was pasted, as a bare host: https://x.myshopify.com/admin -> x.myshopify.com."""
    value = (value or "").strip().lower()
    value = re.sub(r"^https?://", "", value).strip("/")
    return value.split("/")[0]


def check_domain(value: str) -> str:
    """The store's *.myshopify.com host, or ValidationError: Trammel only ever
    connects to Shopify itself, never to an address someone types in."""
    domain = normalize_domain(value)
    if not _DOMAIN.match(domain):
        raise ValidationError(
            "Enter the store's your-store.myshopify.com address (in the Shopify admin, "
            "Settings, Domains), not the domain customers use."
        )
    return domain


def webhook_secret(config: ShopifySettings) -> str:
    """What Shopify signs webhooks with: the API secret key for a custom app,
    the client secret for a Dev Dashboard app (either, if only one is set)."""
    api_secret, client_secret = secrets.get(API_SECRET_KEY), secrets.get(CLIENT_SECRET_KEY)
    if config.auth_mode == ShopifySettings.Auth.CLIENT:
        return client_secret or api_secret or ""
    return api_secret or client_secret or ""


def take_webhook(body: bytes, signature: str, shop: str) -> int:
    """An order webhook, checked and queued: the HTTP status to answer with.
    Nothing is read before Shopify's signature (an HMAC of the exact body)
    checks out. Also takes webhooks relayed by the portal on Cloudflare."""
    from trammel.core import tasks

    config = ShopifySettings.load()
    if not (config.enabled and config.webhooks_enabled and webhook_secret(config)):
        return 404
    if not verify_webhook(config, body, signature):
        return 401
    if (shop or "").lower() != normalize_domain(config.shop_domain):
        return 401
    try:
        order = json.loads(body)
    except ValueError:
        return 400
    if not isinstance(order, dict) or not order.get("id"):
        return 200  # nothing to import; don't make Shopify retry
    tasks.enqueue("imports.shopify_order", {"order": order})
    return 200


def verify_webhook(config: ShopifySettings, body: bytes, signature: str) -> bool:
    """X-Shopify-Hmac-Sha256: base64 HMAC-SHA256 of the exact body, constant-time."""
    secret = webhook_secret(config)
    if not secret or not signature:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Talking to Shopify
# ---------------------------------------------------------------------------


def _request(method: str, url: str, *, data: dict | None = None, headers: dict | None = None):
    """One HTTPS request to the store. Returns (parsed JSON, response headers)."""
    from trammel.core.models import SystemSettings

    if SystemSettings.load().is_offline:
        raise ShopifyError("Outbound traffic is switched off in System settings.")
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(  # noqa: S310 — only https to *.myshopify.com
        url,
        data=body,
        method=method,
        headers={"User-Agent": f"Trammel/{settings.APP_VERSION}", **(headers or {})},
    )
    from django.core.exceptions import ValidationError

    from trammel.core import outbound

    try:
        # Pinned to a public address, redirects kept to the store (SEC-10, 0.10.6).
        with outbound.open_url(request, timeout=TIMEOUT, same_origin=True) as response:
            return json.loads(response.read() or b"{}"), response.headers
    except ValidationError as exc:
        raise ShopifyError(f"Refusing to reach Shopify: {' '.join(exc.messages)}") from None
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(ValueError):
            detail = json.loads(exc.read() or b"{}").get("errors", "")
        if exc.code == 401:
            raise ShopifyError(
                "Shopify refused the sign-in (401). Check the access token, or the client ID "
                "and secret, in Settings, Online stores."
            ) from None
        if exc.code == 403:
            raise ShopifyError(
                f"Shopify refused the request (403): {detail or 'missing permission'}. The app "
                "needs read_orders, read_customers, read_fulfillments and read_products, "
                "approved on the store."
            ) from None
        raise ShopifyError(f"Shopify answered {exc.code}: {detail or exc.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ShopifyError(f"Couldn't reach Shopify: {getattr(exc, 'reason', exc)}") from None


def _exchange(config: ShopifySettings, domain: str) -> str:
    """Client ID and secret for a 24-hour Admin API token, kept until it is
    nearly out. A token granted no order access is refused here, with the fix."""
    client_secret = secrets.get(CLIENT_SECRET_KEY)
    if not (config.client_id and client_secret):
        raise ShopifyError("Enter the client ID and client secret in Settings, Online stores.")
    payload, _ = _request(
        "POST",
        f"https://{domain}/admin/oauth/access_token",
        data={
            "grant_type": "client_credentials",
            "client_id": config.client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = payload.get("access_token")
    if not token:
        raise ShopifyError("Shopify didn't return an access token. Check the client ID and secret.")
    granted = {s.strip() for s in (payload.get("scope") or "").split(",") if s.strip()}
    if not granted & {"read_orders", "read_all_orders"}:
        held = f"only {', '.join(sorted(granted))}" if granted else "no access at all"
        raise ShopifyError(
            f"Shopify issued a token with {held}. Orders need read_orders. In the Dev "
            "Dashboard, add read_orders, read_customers, read_fulfillments and read_products "
            "to the app's configuration, release that version, then install the app on the "
            "store so the scopes are approved."
        )
    lifetime = int(payload.get("expires_in") or 86400)
    secrets.put(CACHED_TOKEN_KEY, token)
    ShopifySettings.objects.filter(pk=config.pk).update(
        access_token_expires_at=timezone.now() + dt.timedelta(seconds=lifetime)
    )
    return token


def access_token(config: ShopifySettings) -> str:
    """A usable Admin API token, for whichever way of signing in is set."""
    domain = check_domain(config.shop_domain)
    if config.auth_mode == ShopifySettings.Auth.TOKEN:
        token = secrets.get(TOKEN_KEY)
        if not token:
            raise ShopifyError("Enter the Admin API access token in Settings, Online stores.")
        return token
    cached = secrets.get(CACHED_TOKEN_KEY)
    expires = config.access_token_expires_at
    if cached and expires and timezone.now() + TOKEN_MARGIN < expires:
        return cached
    return _exchange(config, domain)


def forget_token() -> None:
    """Drop the cached 24-hour token, after the credentials change."""
    secrets.clear(CACHED_TOKEN_KEY)
    ShopifySettings.objects.update(access_token_expires_at=None)


def _next_page(link: str | None) -> str | None:
    """Shopify pages by a Link header; its page_info cursor carries the filters."""
    for part in (link or "").split(","):
        bits = part.split(";")
        if len(bits) >= 2 and 'rel="next"' in bits[1].replace(" ", "").replace("'", '"'):
            return bits[0].strip().strip("<>")
    return None


def fetch(config: ShopifySettings, start: dt.date, end: dt.date) -> list[dict]:
    """Every order created from `start` through `end` (shop's own dates), raw."""
    domain = check_domain(config.shop_domain)
    headers = {"X-Shopify-Access-Token": access_token(config), "Accept": "application/json"}
    query = urllib.parse.urlencode(
        {
            "status": "any",
            "created_at_min": f"{start.isoformat()}T00:00:00",
            "created_at_max": f"{end.isoformat()}T23:59:59",
            "limit": 250,
        }
    )
    url = f"https://{domain}/admin/api/{api_version()}/orders.json?{query}"
    orders: list[dict] = []
    for _ in range(MAX_PAGES):
        payload, response_headers = _request("GET", url, headers=headers)
        orders.extend(payload.get("orders") or [])
        url = _next_page(response_headers.get("Link"))
        if not url:
            break
        if not url.startswith(f"https://{domain}/"):
            raise ShopifyError("Shopify's next-page link pointed somewhere else; stopped.")
    return orders


# ---------------------------------------------------------------------------
# Reading an order
# ---------------------------------------------------------------------------


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
    return moment if timezone.is_aware(moment) else timezone.make_aware(moment)


def _shipping(order: dict) -> Decimal:
    shop = ((order.get("total_shipping_price_set") or {}).get("shop_money")) or {}
    if shop.get("amount") is not None:
        return _money(shop["amount"])
    return sum(
        (_money(line.get("price")) for line in order.get("shipping_lines") or []), Decimal(0)
    )


def read(order: dict) -> dict:
    """A Shopify order as the importer reads every store's: plain values, no
    Shopify field names."""
    address = order.get("shipping_address") or order.get("billing_address") or {}
    buyer = order.get("customer") or {}
    name = (
        address.get("name")
        or " ".join(filter(None, [address.get("first_name"), address.get("last_name")]))
        or " ".join(filter(None, [buyer.get("first_name"), buyer.get("last_name")]))
    )
    customer = {
        "name": name or "",
        "email": (order.get("email") or buyer.get("email") or "").strip(),
        "company": address.get("company") or "",
        "phone": address.get("phone") or buyer.get("phone") or "",
        "line1": address.get("address1") or "",
        "line2": address.get("address2") or "",
        "city": address.get("city") or "",
        # province_code is the two-letter state, which tax lookups use.
        "region": address.get("province_code") or address.get("province") or "",
        "postal_code": address.get("zip") or "",
        "country": (address.get("country_code") or "")[:2],
    }
    carrier = tracking = ""
    shipped_on = None
    fulfillments = order.get("fulfillments") or []
    if fulfillments:
        last = fulfillments[-1]
        tracking = last.get("tracking_number") or ""
        carrier = (last.get("tracking_company") or "").strip()
        when = _when(last.get("created_at"))
        shipped_on = timezone.localtime(when).date() if when else None
    lines = [
        {
            "sku": (line.get("sku") or "").strip(),
            "title": line.get("title") or line.get("name") or "Item",
            "variant": line.get("variant_title") or "",
            "quantity": str(int(line.get("quantity") or 0)),
            "unit_price": str(_money(line.get("price"))),
        }
        for line in order.get("line_items") or []
        if int(line.get("quantity") or 0) > 0
    ]
    return {
        "external_order_id": str(order.get("id") or ""),
        "order_number": order.get("name") or str(order.get("order_number") or order.get("id")),
        "order_date": _when(order.get("created_at")),
        "buyer_id": str(buyer.get("id") or ""),
        "test": bool(order.get("test")),
        "cancelled_at": _when(order.get("cancelled_at")),
        "financial_status": order.get("financial_status") or "",
        "customer": customer,
        # Name, email, phone and address are Shopify's protected customer data;
        # below the Advanced plan a store-admin custom app gets none of them.
        "customer_unavailable": not any(
            [customer["email"], name, customer["line1"], customer["postal_code"]]
        ),
        "lines": lines,
        "discount": str(_money(order.get("total_discounts"))),
        "shipping": str(_shipping(order)),
        "tax": str(_money(order.get("total_tax"))),
        "total": str(_money(order.get("total_price"))),
        "taxes_included": bool(order.get("taxes_included")),
        "outstanding": str(_money(order.get("total_outstanding"))),
        "carrier": carrier,
        "tracking": tracking,
        "shipped_on": shipped_on,
        "notes": order.get("note") or "",
    }
