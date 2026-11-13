"""PayPal's Invoicing API (v2), for online payments (0.10.1).

Each Trammel invoice that can be paid online gets one PayPal invoice, numbered
exactly like it, "sent" without emailing anyone so it has a payer page. When
the balance changes, the same PayPal invoice is updated; when it's no longer
owed, it's cancelled. Trammel asks PayPal which were paid; PayPal never calls
Trammel. Outbound HTTPS only, with the app's client ID and secret.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import secrets as random
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.cache import cache

from trammel.core import secrets

from .errors import ProviderError

LIVE = "https://api-m.paypal.com"
SANDBOX = "https://api-m.sandbox.paypal.com"
SECRET_KEY = "payments.paypal_secret"
TIMEOUT = 30
MAX_PAGES = 20
#: Statuses that mean the customer paid through PayPal. MARKED_AS_PAID is the
#: shop recording a payment by hand in PayPal: not money through PayPal.
PAID = ("PAID",)
#: Nobody is told anything PayPal does here.
QUIET = {"send_to_recipient": False, "send_to_invoicer": False}
#: The scope PayPal's sign-in must list for any of the Invoicing calls.
INVOICING = "https://uri.paypal.com/services/invoicing"


class PayPalError(ProviderError):
    """PayPal refused or couldn't be reached; the message says what to do."""

    issue: str = ""
    detail: str = ""
    debug_id: str = ""


def _config():
    from .models import PayPalSettings

    row = PayPalSettings.load()
    secret = secrets.get(SECRET_KEY)
    if not (row.client_id and secret):
        raise PayPalError("Enter the PayPal client ID and secret in Settings, Online payments.")
    return row, secret


def _base(row) -> str:
    return SANDBOX if row.sandbox else LIVE


def _key(row) -> str:
    where = "sandbox" if row.sandbox else "live"
    return f"payments.paypal.invoicing-token:{where}:{row.client_id}"


def _keep(row, found: dict) -> str:
    token = found["access_token"]
    cache.set(_key(row), token, max(60, int(found.get("expires_in") or 3600) - 120))
    return token


def _token(row, secret, *, fresh=False) -> str:
    if not fresh and (token := cache.get(_key(row))):
        return token
    return _keep(row, _sign_in(row, secret))


def _sign_in(row, secret) -> dict:
    """PayPal's token answer: access_token, scope (space-separated), expires_in.

    Invoicing is asked for by name. PayPal's default sign-in for an app can
    leave it out even when the app has it ticked (seen 11/12/2026: the default
    token listed ~30 scopes but not invoicing, and PayPal kept handing out that
    same token; asking for the scope gave a new token that has it)."""
    basic = base64.b64encode(f"{row.client_id}:{secret}".encode()).decode()
    return _send(
        "POST",
        _base(row) + "/v1/oauth2/token",
        body=urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": INVOICING}
        ).encode(),
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        signing_in=True,
    )


def _explain(body: bytes) -> tuple[str, str, str]:
    """(issue, readable message, debug id) from PayPal's error body."""
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return "", "", ""
    details = data.get("details") or []
    issue = details[0].get("issue", "") if details and isinstance(details[0], dict) else ""
    text = " ".join(
        str(d.get("description") or d.get("issue") or "") for d in details if isinstance(d, dict)
    )
    message = data.get("message") or data.get("error_description") or data.get("name") or ""
    issue = issue or data.get("name", "") or str(data.get("error") or "")
    return issue, f"{message} {text}".strip(), data.get("debug_id", "")


def _send(method, url, *, body=None, headers=None, signing_in=False) -> dict:
    from trammel.core.models import SystemSettings

    if SystemSettings.load().is_offline:
        raise PayPalError("Outbound traffic is switched off in System settings.")
    request = urllib.request.Request(  # noqa: S310 — PayPal's fixed https addresses
        url,
        data=body,
        method=method,
        headers={"User-Agent": f"Trammel/{settings.APP_VERSION}", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        issue, detail, debug_id = _explain(exc.read())
        debug_id = debug_id or (exc.headers.get("Paypal-Debug-Id", "") if exc.headers else "")
        if signing_in and issue == "invalid_scope":
            message = (
                "PayPal won't give the app Invoicing. On developer.paypal.com, Apps & "
                "Credentials (Sandbox or Live, as set here), open the app, tick Invoicing "
                "under its features, and Save."
            )
        elif exc.code == 401 and signing_in:
            message = (
                "PayPal didn't accept the client ID and secret. Check them, and that sandbox "
                "is set to match (Settings, Online payments)."
            )
        elif exc.code == 403:
            message = f"PayPal didn't allow the app to do this ({detail or 'no reason given'}). "
            message += "Test it in Settings, Online payments, to see why."
        else:
            message = f"PayPal answered {exc.code}: {detail or exc.reason}"
        if debug_id:
            message += f" PayPal debug ID: {debug_id}."
        error = PayPalError(message)
        error.status, error.issue, error.debug_id = exc.code, issue, debug_id
        error.detail = detail
        raise error from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PayPalError(f"Couldn't reach PayPal: {getattr(exc, 'reason', exc)}") from None
    except ValueError:
        raise PayPalError("PayPal sent back something that isn't JSON.") from None


def call(method: str, path: str, data=None, *, query: dict | None = None) -> dict:
    """One API call, signed in; its JSON. Raises PayPalError."""
    row, secret = _config()
    url = _base(row) + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
    body = json.dumps(data).encode() if data is not None else None
    extra = {"Content-Type": "application/json", "Prefer": "return=representation"}
    if method == "POST":
        extra["PayPal-Request-Id"] = random.token_hex(16)
    for attempt in (1, 2):
        token = _token(row, secret, fresh=attempt == 2)
        try:
            return _send(
                method, url, body=body, headers={"Authorization": f"Bearer {token}", **extra}
            )
        except PayPalError as exc:
            if exc.status != 401 or attempt == 2:
                raise
    return {}


def signed_in() -> dict:
    """A fresh sign-in, kept for the calls that follow: {"scopes": [...]}."""
    row, secret = _config()
    found = _sign_in(row, secret)
    _keep(row, found)
    return {"scopes": str(found.get("scope") or "").split()}


def check_credentials() -> None:
    """Raise PayPalError unless the app can sign in and read invoices; when it
    can't, say whether PayPal left Invoicing out of the app's sign-in or
    refused the account itself."""
    row, _ = _config()
    where = "Sandbox" if row.sandbox else "Live"
    app = f"the app whose client ID ends …{row.client_id[-6:]}"
    granted = signed_in()
    if granted["scopes"] and INVOICING not in granted["scopes"]:
        raise PayPalError(
            f"PayPal signs the app in but hasn't given it Invoicing. On developer.paypal.com, "
            f"Apps & Credentials, {where}: open {app}, tick Invoicing under its features, "
            f"and Save, then Test again."
        )
    try:
        call("GET", "/v2/invoicing/invoices", query={"page": 1, "page_size": 1})
    except PayPalError as exc:
        if exc.status != 403:
            raise
        refused = f"PayPal refused to list the app's invoices ({exc.detail or 'no reason given'})."
        if granted["scopes"]:
            message = (
                f"{refused} Its sign-in does include Invoicing, which points at the PayPal "
                "account the app belongs to rather than the app: it has to be a business "
                "account that can send invoices. In sandbox, check the app's sandbox account "
                "(Testing Tools, Sandbox Accounts) is a Business one, or make a new Business "
                "sandbox account and an app under it."
            )
        else:
            message = (
                f"{refused} On developer.paypal.com, Apps & Credentials, {where}: check {app} "
                "has Invoicing ticked under Features, and Save."
            )
        if exc.debug_id:
            message += f" PayPal debug ID, for PayPal's support: {exc.debug_id}."
        error = PayPalError(message)
        error.status, error.issue, error.debug_id = exc.status, exc.issue, exc.debug_id
        raise error from None


def _body(
    *, number: str, reference: str, note: str, memo: str, name: str, value: str, currency: str
) -> dict:
    detail = {"reference": reference, "currency_code": currency, "note": note, "memo": memo}
    if number:
        detail["invoice_number"] = number
    return {
        "detail": detail,
        "items": [
            {
                "name": name[:200],
                "quantity": "1",
                "unit_amount": {"currency_code": currency, "value": value},
            }
        ],
        "configuration": {"allow_tip": False, "partial_payment": {"allow_partial_payment": False}},
    }


def payer_url(found: dict) -> str:
    return ((found.get("detail") or {}).get("metadata") or {}).get("recipient_view_url", "")


def create_invoice(
    *, number: str, reference: str, note: str, memo: str, name: str, value: str, currency: str
) -> dict:
    """A PayPal invoice for `value`, sent quietly so it has a payer page:
    {id, url, number}. Numbered `number`, or by PayPal when that's taken."""
    fields = {
        "reference": reference,
        "note": note,
        "memo": memo,
        "name": name,
        "value": value,
        "currency": currency,
    }
    try:
        made = call("POST", "/v2/invoicing/invoices", _body(number=number, **fields))
    except PayPalError as exc:
        if exc.issue != "DUPLICATE_INVOICE_NUMBER" or not number:
            raise
        # Never a look-alike of a Trammel number: PayPal numbers it itself.
        made = call("POST", "/v2/invoicing/invoices", _body(number="", **fields))
    invoice_id = made["id"]
    sent = call("POST", f"/v2/invoicing/invoices/{urllib.parse.quote(invoice_id)}/send", QUIET)
    found = call("GET", f"/v2/invoicing/invoices/{urllib.parse.quote(invoice_id)}")
    url = payer_url(found) or (sent or {}).get("href", "")
    if not url.startswith("https://"):
        raise PayPalError("PayPal didn't give the invoice a payer page.")
    return {
        "id": invoice_id,
        "url": url,
        "number": (found.get("detail") or {}).get("invoice_number", ""),
    }


def update_amount(invoice_id: str, *, value: str, currency: str) -> None:
    """The same PayPal invoice, for a new amount; nobody is told."""
    path = f"/v2/invoicing/invoices/{urllib.parse.quote(invoice_id)}"
    found = call("GET", path)
    found["items"] = [
        {
            **(found.get("items") or [{}])[0],
            "quantity": "1",
            "unit_amount": {"currency_code": currency, "value": value},
        }
    ]
    for key in (
        "amount",
        "due_amount",
        "links",
        "payments",
        "refunds",
        "gratuity",
        "effective_invoice_total",
        "effective_due_amount",
    ):
        found.pop(key, None)
    call("PUT", path, found, query={k: "false" for k in QUIET})


#: A PayPal invoice in one of these has money on its way: it can't be
#: cancelled, and the invoice mustn't be offered anywhere else meanwhile.
BEING_PAID = ("PAID", "PAYMENT_PENDING", "PARTIALLY_PAID", "MARKED_AS_PAID")


def cancel(invoice_id: str) -> str:
    """Cancel quietly; "" when done (or already cancelled, or gone). When PayPal
    refuses, the PayPal invoice's status ("PAID", "PAYMENT_PENDING", …)."""
    try:
        call("POST", f"/v2/invoicing/invoices/{urllib.parse.quote(invoice_id)}/cancel", QUIET)
    except PayPalError as exc:
        if exc.status == 404:
            return ""
        if exc.status != 422:
            raise
        status = invoice(invoice_id).get("status", "")
        return status if status in BEING_PAID else ""
    return ""


def _stamp(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def paid_since(start: dt.datetime, end: dt.datetime) -> list[dict]:
    """PayPal invoices paid between `start` and `end`."""
    found: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        result = call(
            "POST",
            "/v2/invoicing/search-invoices",
            {
                "status": list(PAID),
                "payment_date_range": {"start": _stamp(start), "end": _stamp(end)},
            },
            query={"page": page, "page_size": 100, "total_required": "true"},
        )
        found += result.get("items") or []
        if page >= int(result.get("total_pages") or 1):
            break
    return found


def invoice(invoice_id: str) -> dict:
    return call("GET", f"/v2/invoicing/invoices/{urllib.parse.quote(invoice_id)}")
