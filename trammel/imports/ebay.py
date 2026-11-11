"""eBay: connecting a seller account, fetching its orders, and reading them
(ported from sales_tracker).

Reading orders needs a user token, so the seller approves Trammel on eBay
(authorization code grant, FR-IMP-5):

1. "Connect eBay" sends the browser to eBay with a one-time state. eBay wants
   the app's RuName (its name for the redirect) where other sites take the
   redirect address itself.
2. The seller approves; eBay sends the browser to the RuName's "auth accepted"
   address, which should be Trammel's callback, with a code. When that can't
   reach Trammel, the seller pastes the address (or the code) into Settings,
   Online stores instead.
3. The code becomes a two-hour access token and a refresh token that lasts
   about 18 months, both kept encrypted. Token calls authenticate the app with
   its App ID and Cert ID (HTTP Basic).

Orders come from the Sell Fulfillment API with the tax breakdown, so eBay's
"collect and remit" tax is part of the total. eBay is the marketplace
facilitator in every US state with a sales tax: it files and pays that tax,
and the sale records it as paid by eBay.

eBay's account deletion notices (FR-IMP-6) are handled in deletions.py.
"""

from __future__ import annotations

import base64
import datetime as dt
import hmac
import json
import secrets as random
import urllib.parse
from decimal import Decimal

from django.utils import timezone

from trammel.core import secrets

from .http import HttpProblem, StoreError, call, pasted_code
from .models import EbaySettings
from .regions import state_code

CERT_ID_KEY = "imports.ebay.cert_id"
ACCESS_TOKEN_KEY = "imports.ebay.access_token"
REFRESH_TOKEN_KEY = "imports.ebay.refresh_token"
#: The state between "Connect" and eBay's answer.
PENDING_KEY = "imports.ebay.pending_connection"

SCOPES = ("https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",)
TOKEN_MARGIN = dt.timedelta(minutes=5)
PENDING_LIFETIME = dt.timedelta(minutes=30)
PAGE = 200
MAX_PAGES = 100
CENT = Decimal("0.01")

EbayError = StoreError
code_from = pasted_code


def host(config: EbaySettings, sub: str) -> str:
    """https://api.ebay.com, or the sandbox's https://api.sandbox.ebay.com."""
    return f"https://{sub}.{'sandbox.' if config.sandbox else ''}ebay.com"


def token_url(config: EbaySettings) -> str:
    return f"{host(config, 'api')}/identity/v1/oauth2/token"


def _request(method: str, url: str, *, data: dict | None = None, headers: dict | None = None):
    """One call to eBay, its errors said in words a seller can act on."""
    try:
        return call("eBay", method, url, data=data, headers=headers)
    except HttpProblem as exc:
        if exc.error == "invalid_grant" or ("/identity/" in url and exc.status == 400):
            raise EbayError(
                "eBay no longer accepts this connection (it expired or was removed). Connect "
                f"eBay again in Settings, Online stores. eBay said: {exc.detail}"
            ) from None
        if exc.error == "invalid_client" or exc.status == 401:
            raise EbayError(
                f"eBay refused the sign-in ({exc.status}): {exc.detail}. Check the App ID and "
                "Cert ID (production keys unless the sandbox is ticked), then connect again."
            ) from None
        if exc.status == 403:
            raise EbayError(
                f"eBay refused the request (403): {exc.detail}. The connection needs access to "
                "orders (sell.fulfillment.readonly); connect eBay again to approve it."
            ) from None
        raise EbayError(f"eBay answered {exc.status}: {exc.detail}") from None


# ---------------------------------------------------------------------------
# Connecting (FR-IMP-5)
# ---------------------------------------------------------------------------


def _app(config: EbaySettings) -> tuple[str, str]:
    client_id, cert = (config.client_id or "").strip(), secrets.get(CERT_ID_KEY) or ""
    if not (client_id and cert and (config.ru_name or "").strip()):
        raise EbayError(
            "Enter the App ID, Cert ID and RuName in Settings, Online stores. eBay needs all three."
        )
    return client_id, cert


def begin(config: EbaySettings) -> str:
    """The address on eBay where the seller approves Trammel."""
    client_id, _ = _app(config)
    state = random.token_urlsafe(24)
    secrets.put(
        PENDING_KEY,
        json.dumps({"state": state, "until": (timezone.now() + PENDING_LIFETIME).isoformat()}),
    )
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": config.ru_name.strip(),
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
        }
    )
    return f"{host(config, 'auth')}/oauth2/authorize?{query}"


def complete(config: EbaySettings, code: str, state: str | None) -> EbaySettings:
    """Turn the code eBay gave back into tokens. `state` is checked whenever
    there is one (always, through the callback address)."""
    raw = secrets.get(PENDING_KEY)
    if not raw:
        raise EbayError("Press Connect eBay first, then approve Trammel on eBay.")
    pending = json.loads(raw)
    if dt.datetime.fromisoformat(pending["until"]) < timezone.now():
        secrets.clear(PENDING_KEY)
        raise EbayError("That approval took too long. Press Connect eBay again.")
    if state is not None and not hmac.compare_digest(str(state), pending["state"]):
        raise EbayError(
            "eBay's answer doesn't match the last Connect eBay. Press Connect eBay again."
        )
    if not code:
        raise EbayError("Paste the code eBay gave, or the whole address it sent you to.")
    _token(
        config,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": config.ru_name.strip()},
    )
    secrets.clear(PENDING_KEY)
    EbaySettings.objects.filter(pk=config.pk).update(connected_at=timezone.now())
    config.refresh_from_db()
    return config


def _token(config: EbaySettings, data: dict) -> str:
    client_id, cert = _app(config)
    basic = base64.b64encode(f"{client_id}:{cert}".encode()).decode()
    payload, _ = _request(
        "POST",
        token_url(config),
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Authorization": f"Basic {basic}",
        },
    )
    token = payload.get("access_token")
    if not token:
        raise EbayError("eBay didn't return an access token. Connect eBay again.")
    now = timezone.now()
    secrets.put(ACCESS_TOKEN_KEY, token)
    lifetime = dt.timedelta(seconds=int(payload.get("expires_in") or 7200))
    updates = {"access_token_expires_at": now + lifetime}
    if payload.get("refresh_token"):
        secrets.put(REFRESH_TOKEN_KEY, payload["refresh_token"])
        seconds = int(payload.get("refresh_token_expires_in") or 0)
        updates["refresh_token_expires_at"] = (
            now + dt.timedelta(seconds=seconds) if seconds else None
        )
    EbaySettings.objects.filter(pk=config.pk).update(**updates)
    return token


def access_token(config: EbaySettings) -> str:
    """A usable user token, renewed from the refresh token when it is stale."""
    cached = secrets.get(ACCESS_TOKEN_KEY)
    expires = config.access_token_expires_at
    if cached and expires and timezone.now() + TOKEN_MARGIN < expires:
        return cached
    refresh = secrets.get(REFRESH_TOKEN_KEY)
    if not refresh or not config.connected_at:
        raise EbayError("eBay isn't connected yet. Connect it in Settings, Online stores.")
    if config.has_expired:
        raise EbayError(
            "The eBay connection has expired (eBay keeps one for about 18 months). Connect eBay "
            "again in Settings, Online stores."
        )
    # eBay wants the scopes restated when renewing.
    return _token(
        config,
        {"grant_type": "refresh_token", "refresh_token": refresh, "scope": " ".join(SCOPES)},
    )


def disconnect() -> None:
    for key in (ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, PENDING_KEY):
        secrets.clear(key)
    EbaySettings.objects.update(
        connected_at=None, access_token_expires_at=None, refresh_token_expires_at=None
    )


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _utc(day: dt.date, end: bool = False) -> str:
    moment = timezone.make_aware(dt.datetime.combine(day, dt.time.max if end else dt.time.min))
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.") + ("999Z" if end else "000Z")


def fetch(config: EbaySettings, start: dt.date, end: dt.date) -> list[dict]:
    """Every order created from `start` through `end` (local dates), raw."""
    headers = {"Authorization": f"Bearer {access_token(config)}", "Accept": "application/json"}
    orders: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        query = urllib.parse.urlencode(
            {
                "filter": f"creationdate:[{_utc(start)}..{_utc(end, end=True)}]",
                # With this, eBay's collect-and-remit tax is part of the total.
                "fieldGroups": "TAX_BREAKDOWN",
                "limit": PAGE,
                "offset": offset,
            }
        )
        payload, _ = _request(
            "GET", f"{host(config, 'api')}/sell/fulfillment/v1/order?{query}", headers=headers
        )
        batch = payload.get("orders") or []
        orders.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return orders


# ---------------------------------------------------------------------------
# Reading an order
# ---------------------------------------------------------------------------


def money(value) -> Decimal:
    """eBay's {"value": "12.50", "currency": "USD"} as 12.50."""
    if not isinstance(value, dict):
        return Decimal("0")
    try:
        return Decimal(str(value.get("value") or "0"))
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


STATUS = {
    "PAID": "paid",
    "FULLY_REFUNDED": "refunded",
    "PARTIALLY_REFUNDED": "partially_refunded",
}


def read(order: dict) -> dict:
    """An eBay order as the importer reads every store's."""
    ship_to: dict = {}
    for instruction in order.get("fulfillmentStartInstructions") or []:
        ship_to = (instruction.get("shippingStep") or {}).get("shipTo") or {}
        if ship_to:
            break
    buyer = order.get("buyer") or {}
    registered = buyer.get("buyerRegistrationAddress") or {}
    address = ship_to.get("contactAddress") or registered.get("contactAddress") or {}
    country = (address.get("countryCode") or "")[:2].upper()
    name = (ship_to.get("fullName") or registered.get("fullName") or "").strip()
    phone = (ship_to.get("primaryPhone") or registered.get("primaryPhone") or {}).get("phoneNumber")
    customer = {
        "name": name,
        "email": (ship_to.get("email") or registered.get("email") or "").strip(),
        "company": (ship_to.get("companyName") or "").strip(),
        "phone": phone or "",
        "line1": address.get("addressLine1") or "",
        "line2": address.get("addressLine2") or "",
        "city": address.get("city") or "",
        "region": state_code(address.get("stateOrProvince"), country or "US"),
        "postal_code": address.get("postalCode") or "",
        "country": country,
    }
    lines, items, remitted = [], Decimal("0"), Decimal("0")
    for line in order.get("lineItems") or []:
        quantity = int(line.get("quantity") or 0)
        if quantity < 1:
            continue
        # lineItemCost is the line's price before discounts: unit price × quantity.
        cost = money(line.get("lineItemCost"))
        items += cost
        remitted += sum(
            (money(t.get("amount")) for t in line.get("ebayCollectAndRemitTaxes") or []),
            Decimal("0"),
        )
        variant = ", ".join(
            f"{a.get('name')}: {a.get('value')}"
            for a in line.get("variationAspects") or []
            if a.get("value")
        )
        lines.append(
            {
                "sku": (line.get("sku") or "").strip(),
                "title": line.get("title") or "Item",
                "variant": variant,
                "quantity": str(quantity),
                "unit_price": str((cost / quantity).quantize(CENT)),
            }
        )
    pricing = order.get("pricingSummary") or {}
    discount = abs(money(pricing.get("priceDiscount")))
    shipping = money(pricing.get("deliveryCost")) - abs(money(pricing.get("deliveryDiscount")))
    reported_tax = money(pricing.get("tax"))
    # eBay collects and pays US sales tax itself. Its pricing summary may repeat
    # that tax, or (sellers outside it) be tax the seller collected.
    tax = (
        remitted
        if abs(reported_tax - remitted) <= CENT or not reported_tax
        else (reported_tax + remitted)
    )
    total = money(pricing.get("total"))
    without = items - discount + shipping + tax - remitted
    if remitted and abs(total - without) <= CENT:
        total += remitted  # the total came without eBay's own tax
    carrier = tracking = ""
    shipped_on = None
    for fulfillment in order.get("shippingFulfillments") or []:
        tracking = fulfillment.get("shipmentTrackingNumber") or tracking
        carrier = (fulfillment.get("shippingCarrierCode") or carrier or "").strip()
        when = _when(fulfillment.get("shippedDate"))
        if when:
            shipped_on = timezone.localtime(when).date()
    cancel = order.get("cancelStatus") or {}
    cancelled = None
    if cancel.get("cancelState") == "CANCELED" or order.get("orderPaymentStatus") == "FAILED":
        cancelled = _when(cancel.get("cancelledDate")) or timezone.now()
    order_id = str(order.get("orderId") or "")
    return {
        "external_order_id": order_id,
        "order_number": order_id,
        "order_date": _when(order.get("creationDate")),
        "buyer_id": str(buyer.get("username") or ""),
        "test": False,
        "cancelled_at": cancelled,
        "financial_status": STATUS.get(order.get("orderPaymentStatus") or "", "pending"),
        "customer": customer,
        # eBay withholds the buyer's details on some orders.
        "customer_unavailable": not any(
            [customer["email"], name, customer["line1"], customer["postal_code"]]
        ),
        "lines": lines,
        "discount": str(discount),
        "shipping": str(shipping),
        "tax": str(tax),
        # eBay, as the marketplace facilitator, files and pays the sales tax.
        "tax_remitted_by": "eBay" if tax else "",
        "total": str(total),
        "taxes_included": False,
        "outstanding": "0",
        "carrier": carrier,
        "tracking": tracking,
        "shipped_on": shipped_on,
        "notes": (order.get("buyerCheckoutNotes") or "").strip(),
    }
