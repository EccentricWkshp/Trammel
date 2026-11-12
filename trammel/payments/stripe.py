"""Stripe's API, for online payments (0.10.0): payment links made and turned
off, and completed checkouts read back. Outbound HTTPS only, with the secret
key the admin pasted in; Stripe never calls Trammel."""

from __future__ import annotations

import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

from trammel.core import secrets

from .errors import ProviderError

API = "https://api.stripe.com/v1"
KEY = "payments.stripe_key"
#: The API version this is written for, so Stripe's answers keep their shape.
VERSION = "2026-10-28.endive"
TIMEOUT = 30
#: A runaway guard when reading checkouts: 50 pages of 100.
MAX_PAGES = 50
#: What a restricted key needs, as Stripe's key page names them.
PERMISSIONS = (
    ("Payment Links", "Write"),
    ("Checkout Sessions", "Read"),
    ("Products", "Write"),
    ("Prices", "Write"),
)


class StripeError(ProviderError):
    """Stripe refused or couldn't be reached; the message says what to do."""


def test_mode(key: str | None = None) -> bool:
    """A test key (sk_test_…, rk_test_…): no real money moves."""
    key = key if key is not None else (secrets.get(KEY) or "")
    return "_test_" in key


def _form(data, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: a[b][0][c]=…"""
    pairs: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            pairs += _form(value, f"{prefix}[{key}]" if prefix else str(key))
    elif isinstance(data, (list, tuple)):
        for i, value in enumerate(data):
            pairs += _form(value, f"{prefix}[{i}]")
    elif isinstance(data, bool):
        pairs.append((prefix, "true" if data else "false"))
    elif data is not None:
        pairs.append((prefix, str(data)))
    return pairs


def call(method: str, path: str, data: dict | None = None, *, key: str | None = None) -> dict:
    """One API call; its JSON. Raises StripeError."""
    from trammel.core.models import SystemSettings

    if SystemSettings.load().is_offline:
        raise StripeError("Outbound traffic is switched off in System settings.")
    key = key or secrets.get(KEY)
    if not key:
        raise StripeError("Enter the Stripe secret key in Settings, Online payments.")
    url = API + path
    body = None
    if data is not None and method == "GET":
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(_form(data))
    elif data is not None:
        body = urllib.parse.urlencode(_form(data)).encode()
    request = urllib.request.Request(  # noqa: S310 — a fixed https address
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Stripe-Version": VERSION,
            "User-Agent": f"Trammel/{settings.APP_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = (json.loads(exc.read() or b"{}").get("error") or {}).get("message", "")
        except ValueError:
            detail = ""
        if exc.code == 401:
            message = "Stripe didn't accept the secret key. Check it in Settings, Online payments."
        elif exc.code == 403:
            message = (
                f"The key lacks a permission Trammel needs: {detail or 'see Stripe'}. A "
                "restricted key needs " + ", ".join(f"{n} {a}" for n, a in PERMISSIONS) + "."
            )
        else:
            message = f"Stripe answered {exc.code}: {detail or exc.reason}"
        error = StripeError(message)
        error.status = exc.code
        raise error from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise StripeError(f"Couldn't reach Stripe: {getattr(exc, 'reason', exc)}") from None
    except ValueError:
        raise StripeError("Stripe sent back something that isn't JSON.") from None


def check_key(key: str | None = None) -> None:
    """Raise StripeError unless the key works for payment links."""
    call("GET", "/payment_links", {"limit": 1}, key=key)


def create_link(
    *, name: str, description: str, minor: int, currency: str, metadata: dict, return_url: str
) -> dict:
    """A single-use payment link for `minor` units: {id, url}."""
    data = {
        "line_items": [
            {
                "price_data": {
                    "currency": currency.lower(),
                    "unit_amount": minor,
                    "product_data": {"name": name},
                },
                "quantity": 1,
            }
        ],
        # Paid once; then Stripe turns it off.
        "restrictions": {"completed_sessions": {"limit": 1}},
        "metadata": metadata,
        "payment_intent_data": {"description": description, "metadata": metadata},
        "submit_type": "pay",
        "inactive_message": "This payment link is no longer in use. Please see your invoice "
        "for the amount due now.",
    }
    if return_url:
        data["after_completion"] = {"type": "redirect", "redirect": {"url": return_url}}
    else:
        data["after_completion"] = {
            "type": "hosted_confirmation",
            "hosted_confirmation": {"custom_message": "Thank you. We've received your payment."},
        }
    found = call("POST", "/payment_links", data)
    return {"id": found["id"], "url": found["url"]}


def deactivate(link_id: str) -> None:
    """Turn a link off. One already gone is passed over."""
    try:
        call("POST", f"/payment_links/{urllib.parse.quote(link_id)}", {"active": False})
    except StripeError as exc:
        if exc.status != 404:
            raise


def completed_checkouts(since: dt.datetime) -> list[dict]:
    """Every completed checkout created at or after `since`, newest first."""
    found: list[dict] = []
    after = None
    for _ in range(MAX_PAGES):
        query = {"status": "complete", "created": {"gte": int(since.timestamp())}, "limit": 100}
        if after:
            query["starting_after"] = after
        page = call("GET", "/checkout/sessions", query)
        rows = page.get("data") or []
        found += rows
        if not page.get("has_more") or not rows:
            break
        after = rows[-1]["id"]
    return found


def checkout(session_id: str) -> dict:
    return call("GET", f"/checkout/sessions/{urllib.parse.quote(session_id)}")
