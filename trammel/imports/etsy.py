"""Etsy: connecting a shop, fetching its receipts, and reading them (ported from
sales_tracker).

Etsy's Open API v3 needs the seller to approve Trammel in a browser (OAuth 2.0
with PKCE, FR-IMP-5):

1. "Connect Etsy" sends the browser to Etsy with a one-time state and a PKCE
   challenge; the verifier never leaves Trammel.
2. The seller approves; Etsy sends the browser back to Trammel's callback
   address with a code. If that address can't be reached (Trammel on another
   machine than the browser), the seller pastes the code, or the whole address
   Etsy sent them to, into Settings, Online stores instead.
3. The code becomes an hour-long access token and a refresh token, both kept
   encrypted. Fetches renew the access token by themselves; Etsy keeps the
   connection for 90 days after the last renewal, after which the seller
   connects again.

Every request also carries the app's x-api-key, which Etsy wants as the
keystring and shared secret joined by a colon (the keystring alone is refused).

One receipt is one checkout: its transactions are the lines. Money comes as
{"amount": 1250, "divisor": 100}. Etsy is the marketplace facilitator: it
collects and pays the sales tax, so the sale records the tax as paid by Etsy.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import secrets as random
import urllib.parse
from decimal import Decimal

from django.utils import timezone

from trammel.core import secrets

from .http import HttpProblem, StoreError, call
from .models import EtsySettings
from .regions import state_code

SHARED_SECRET_KEY = "imports.etsy.shared_secret"
ACCESS_TOKEN_KEY = "imports.etsy.access_token"
REFRESH_TOKEN_KEY = "imports.etsy.refresh_token"
#: The state, PKCE verifier and callback address between "Connect" and Etsy's answer.
PENDING_KEY = "imports.etsy.pending_connection"

AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
API = "https://openapi.etsy.com/v3/application"
SCOPES = ("transactions_r", "listings_r", "shops_r")

#: Renew a little early so a token can't run out partway through an import.
TOKEN_MARGIN = dt.timedelta(minutes=5)
#: Etsy keeps a refresh token for 90 days.
REFRESH_LIFETIME = dt.timedelta(days=90)
#: How long the seller has to approve after pressing Connect.
PENDING_LIFETIME = dt.timedelta(minutes=30)
PAGE = 100
MAX_PAGES = 100

EtsyError = StoreError


def _request(method: str, url: str, *, data: dict | None = None, headers: dict | None = None):
    """One call to Etsy, its errors said in words a seller can act on."""
    try:
        return call("Etsy", method, url, data=data, headers=headers)
    except HttpProblem as exc:
        if exc.error == "invalid_grant" or (url == TOKEN_URL and exc.status in (400, 401)):
            raise EtsyError(
                "Etsy no longer accepts this connection (it expired or was removed). Connect "
                f"Etsy again in Settings, Online stores. Etsy said: {exc.detail}"
            ) from None
        if exc.status == 401:
            raise EtsyError(
                f"Etsy refused the sign-in (401): {exc.detail}. Check the keystring and shared "
                "secret, then connect Etsy again."
            ) from None
        if exc.status == 403:
            raise EtsyError(
                f"Etsy refused the request (403): {exc.detail}. The connection needs "
                f"{', '.join(SCOPES)}; connect Etsy again to approve them."
            ) from None
        raise EtsyError(f"Etsy answered {exc.status}: {exc.detail}") from None


# ---------------------------------------------------------------------------
# Connecting (FR-IMP-5)
# ---------------------------------------------------------------------------


def _credentials(config: EtsySettings) -> tuple[str, str]:
    keystring, shared = (config.keystring or "").strip(), secrets.get(SHARED_SECRET_KEY) or ""
    if not (keystring and shared):
        raise EtsyError(
            "Enter the app's keystring and shared secret in Settings, Online stores. Etsy needs "
            "both."
        )
    return keystring, shared


def begin(config: EtsySettings, redirect_uri: str) -> str:
    """The address on Etsy where the seller approves Trammel."""
    keystring, _ = _credentials(config)
    state = random.token_urlsafe(24)
    verifier = random.token_urlsafe(64)[:128]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode()
        .rstrip("=")
    )
    secrets.put(
        PENDING_KEY,
        json.dumps(
            {
                "state": state,
                "verifier": verifier,
                "redirect_uri": redirect_uri,
                "until": (timezone.now() + PENDING_LIFETIME).isoformat(),
            }
        ),
    )
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": keystring,
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def code_from(pasted: str) -> tuple[str, str | None]:
    """(code, state) from what the seller pasted: the code alone, or the whole
    address Etsy sent them to."""
    text = (pasted or "").strip()
    if "code=" in text:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query or text.split("?")[-1])
        return (query.get("code") or [""])[0].strip(), (query.get("state") or [None])[0]
    return text, None


def complete(config: EtsySettings, code: str, state: str | None) -> EtsySettings:
    """Turn the code Etsy gave back into tokens, and find the shop. `state` is
    checked whenever there is one (always, through the callback address)."""
    raw = secrets.get(PENDING_KEY)
    if not raw:
        raise EtsyError("Press Connect Etsy first, then approve Trammel on Etsy.")
    pending = json.loads(raw)
    if dt.datetime.fromisoformat(pending["until"]) < timezone.now():
        secrets.clear(PENDING_KEY)
        raise EtsyError("That approval took too long. Press Connect Etsy again.")
    if state is not None and not hmac.compare_digest(str(state), pending["state"]):
        raise EtsyError(
            "Etsy's answer doesn't match the last Connect Etsy. Press Connect Etsy again."
        )
    if not code:
        raise EtsyError("Paste the code Etsy gave, or the whole address it sent you to.")
    keystring, shared = _credentials(config)
    _token(
        config,
        {
            "grant_type": "authorization_code",
            "client_id": keystring,
            "client_secret": shared,
            "redirect_uri": pending["redirect_uri"],
            "code": code,
            "code_verifier": pending["verifier"],
        },
    )
    secrets.clear(PENDING_KEY)
    config.refresh_from_db()
    shop_id, shop_name = _find_shop(config)
    now = timezone.now()
    EtsySettings.objects.filter(pk=config.pk).update(
        shop_id=shop_id, shop_name=shop_name, connected_at=now
    )
    config.refresh_from_db()
    return config


def _token(config: EtsySettings, data: dict) -> str:
    payload, _ = _request(
        "POST",
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    token = payload.get("access_token")
    if not token:
        raise EtsyError("Etsy didn't return an access token. Connect Etsy again.")
    now = timezone.now()
    secrets.put(ACCESS_TOKEN_KEY, token)
    updates = {
        "access_token_expires_at": now
        + dt.timedelta(seconds=int(payload.get("expires_in") or 3600))
    }
    if payload.get("refresh_token"):
        secrets.put(REFRESH_TOKEN_KEY, payload["refresh_token"])
        updates["refresh_token_expires_at"] = now + REFRESH_LIFETIME
    EtsySettings.objects.filter(pk=config.pk).update(**updates)
    return token


def access_token(config: EtsySettings) -> str:
    """A usable access token, renewed from the refresh token when it is stale."""
    cached = secrets.get(ACCESS_TOKEN_KEY)
    expires = config.access_token_expires_at
    if cached and expires and timezone.now() + TOKEN_MARGIN < expires:
        return cached
    refresh = secrets.get(REFRESH_TOKEN_KEY)
    if not refresh or not config.connected_at:
        raise EtsyError("Etsy isn't connected yet. Connect it in Settings, Online stores.")
    if config.has_expired:
        raise EtsyError(
            "The Etsy connection has expired (Etsy keeps one for 90 days without use). Connect "
            "Etsy again in Settings, Online stores."
        )
    keystring, shared = _credentials(config)
    return _token(
        config,
        {
            "grant_type": "refresh_token",
            "client_id": keystring,
            "client_secret": shared,
            "refresh_token": refresh,
        },
    )


def disconnect() -> None:
    for key in (ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, PENDING_KEY):
        secrets.clear(key)
    EtsySettings.objects.update(
        connected_at=None,
        access_token_expires_at=None,
        refresh_token_expires_at=None,
        shop_id="",
        shop_name="",
    )


def _headers(config: EtsySettings) -> dict:
    keystring, shared = _credentials(config)
    return {
        "x-api-key": f"{keystring}:{shared}",
        "Authorization": f"Bearer {access_token(config)}",
        "Accept": "application/json",
    }


def _find_shop(config: EtsySettings) -> tuple[str, str]:
    """The approving account's shop: its id (for receipts) and its name."""
    me, _ = _request("GET", f"{API}/users/me", headers=_headers(config))
    shop_id = str(me.get("shop_id") or "")
    if not shop_id:
        raise EtsyError("The Etsy account that approved Trammel has no shop.")
    name = ""
    try:
        shop, _ = _request("GET", f"{API}/shops/{shop_id}", headers=_headers(config))
        name = str(shop.get("shop_name") or "")
    except EtsyError:
        pass  # the name is only for show
    return shop_id, name


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _epoch(day: dt.date, end: bool = False) -> int:
    moment = dt.datetime.combine(day, dt.time.max if end else dt.time.min)
    return int(timezone.make_aware(moment).timestamp())


def fetch(config: EtsySettings, start: dt.date, end: dt.date) -> list[dict]:
    """Every receipt created from `start` through `end` (local dates), raw."""
    if not config.shop_id:
        raise EtsyError("Etsy isn't connected yet. Connect it in Settings, Online stores.")
    receipts: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        query = urllib.parse.urlencode(
            {
                "min_created": _epoch(start),
                "max_created": _epoch(end, end=True),
                "limit": PAGE,
                "offset": offset,
            }
        )
        payload, _ = _request(
            "GET", f"{API}/shops/{config.shop_id}/receipts?{query}", headers=_headers(config)
        )
        batch = payload.get("results") or []
        receipts.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return receipts


# ---------------------------------------------------------------------------
# Reading a receipt
# ---------------------------------------------------------------------------


def money(value) -> Decimal:
    """Etsy's {"amount": 1250, "divisor": 100} as 12.50."""
    if not isinstance(value, dict):
        return Decimal("0")
    try:
        divisor = Decimal(str(value.get("divisor") or 100))
        if not divisor:
            return Decimal("0")
        return (Decimal(str(value.get("amount") or 0)) / divisor).quantize(Decimal("0.01"))
    except ArithmeticError:
        return Decimal("0")


def _when(value) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(int(value), tz=dt.UTC) if value else None
    except TypeError, ValueError, OverflowError:
        return None


def _status(receipt: dict) -> str:
    """Etsy's receipt status in the words the importer uses for every store."""
    status = (receipt.get("status") or "").strip().lower()
    if status == "fully refunded":
        return "refunded"
    if status == "partially refunded":
        return "partially_refunded"
    if receipt.get("is_paid") or status in ("paid", "completed"):
        return "paid"
    return "pending"


def read(receipt: dict) -> dict:
    """An Etsy receipt as the importer reads every store's order."""
    country = (receipt.get("country_iso") or "")[:2].upper()
    name = (receipt.get("name") or "").strip()
    customer = {
        "name": name,
        "email": (receipt.get("buyer_email") or "").strip(),
        "company": "",
        "phone": "",
        "line1": receipt.get("first_line") or "",
        "line2": receipt.get("second_line") or "",
        "city": receipt.get("city") or "",
        "region": state_code(receipt.get("state"), country or "US"),
        "postal_code": receipt.get("zip") or "",
        "country": country,
    }
    carrier = tracking = ""
    shipped_on = None
    for shipment in receipt.get("shipments") or []:
        tracking = shipment.get("tracking_code") or tracking
        carrier = (shipment.get("carrier_name") or carrier or "").strip()
        when = _when(shipment.get("shipment_notification_timestamp"))
        if when:
            shipped_on = timezone.localtime(when).date()
    lines = []
    for line in receipt.get("transactions") or []:
        quantity = int(line.get("quantity") or 0)
        if quantity < 1:
            continue
        variant = ", ".join(
            f"{v.get('formatted_name')}: {v.get('formatted_value')}"
            for v in line.get("variations") or []
            if v.get("formatted_value")
        )
        lines.append(
            {
                "sku": (line.get("sku") or "").strip(),
                "title": line.get("title") or "Item",
                "variant": variant,
                "quantity": str(quantity),
                "unit_price": str(money(line.get("price"))),
            }
        )
    gift_wrap = money(receipt.get("gift_wrap_price"))
    if gift_wrap:
        lines.append(
            {
                "sku": "",
                "title": "Gift wrap",
                "variant": "",
                "quantity": "1",
                "unit_price": str(gift_wrap),
            }
        )
    status = (receipt.get("status") or "").strip().lower()
    cancelled = None
    if status == "canceled":
        cancelled = _when(receipt.get("updated_timestamp") or receipt.get("update_timestamp"))
        cancelled = cancelled or timezone.now()
    notes = (receipt.get("message_from_buyer") or "").strip()
    if receipt.get("is_gift") and receipt.get("gift_message"):
        notes = "\n".join(filter(None, [notes, f"Gift message: {receipt['gift_message']}"]))
    tax = money(receipt.get("total_tax_cost")) + money(receipt.get("total_vat_cost"))
    return {
        "external_order_id": str(receipt.get("receipt_id") or ""),
        "order_number": str(receipt.get("receipt_id") or ""),
        "order_date": _when(receipt.get("created_timestamp") or receipt.get("create_timestamp")),
        "buyer_id": str(receipt.get("buyer_user_id") or ""),
        "test": False,
        "cancelled_at": cancelled,
        "financial_status": _status(receipt),
        "customer": customer,
        "customer_unavailable": not any(
            [customer["email"], name, customer["line1"], customer["postal_code"]]
        ),
        "lines": lines,
        "discount": str(money(receipt.get("discount_amt"))),
        "shipping": str(money(receipt.get("total_shipping_cost"))),
        "tax": str(tax),
        # Etsy, as the marketplace facilitator, files and pays the sales tax.
        "tax_remitted_by": "Etsy",
        "total": str(money(receipt.get("grandtotal"))),
        "taxes_included": False,
        "outstanding": "0",
        "carrier": carrier,
        "tracking": tracking,
        "shipped_on": shipped_on,
        "notes": notes,
    }
