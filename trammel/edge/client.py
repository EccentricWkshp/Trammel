"""Trammel's calls to its edge portal Worker, each signed with the sync key
(the Worker's crypto.verifySync checks them): publish, read the inbox, purge."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets as random
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

from trammel.core import secrets

SYNC_KEY = "edge.sync_key"
TIMEOUT = 60


class EdgeError(Exception):
    """The Worker couldn't be reached or refused; the message says what happened."""


def sync_key() -> str:
    """The key Trammel and the Worker share, made the first time it's wanted."""
    key = secrets.get(SYNC_KEY)
    if not key:
        key = random.token_urlsafe(32)
        secrets.put(SYNC_KEY, key)
    return key


def signature(key: str, stamp: str, method: str, path: str, body: bytes) -> str:
    digest = hashlib.sha256(body or b"").hexdigest()
    message = f"{stamp}\n{method.upper()}\n{path}\n{digest}"
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def access_hash(code: str) -> str:
    """A code from staff as the Worker checks it (FR-PRT-10)."""
    return hmac.new(
        sync_key().encode(), f"portal-access:{code}".encode(), hashlib.sha256
    ).hexdigest()


def call(method, path, *, data=None, body=None, content_type=None, base_url=None, raw=False):
    """One signed call to the Worker; its JSON (or bytes, with `raw`)."""
    from trammel.core.models import SystemSettings

    from .models import EdgeSettings

    if SystemSettings.load().is_offline:
        raise EdgeError("Outbound traffic is switched off in System settings.")
    base = (base_url or EdgeSettings.load().public_url).rstrip("/")
    if not base.startswith("https://"):
        raise EdgeError("The edge portal isn't deployed yet.")
    if data is not None:
        body = json.dumps(data, separators=(",", ":")).encode()
        content_type = "application/json"
    body = body or b""
    stamp = str(int(time.time()))
    headers = {
        "X-Trammel-Time": stamp,
        "X-Trammel-Signature": signature(sync_key(), stamp, method, path, body),
        "User-Agent": f"Trammel/{settings.APP_VERSION}",
    }
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(  # noqa: S310 — https only, checked above
        base + path, data=body if method != "GET" else None, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="replace")
        raise EdgeError(f"The edge portal answered {exc.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EdgeError(f"Couldn't reach the edge portal: {getattr(exc, 'reason', exc)}") from None
    if raw:
        return payload
    try:
        return json.loads(payload or b"{}")
    except ValueError:
        raise EdgeError("The edge portal sent back something that isn't JSON.") from None


def health(base_url=None) -> dict:
    return call("GET", "/_sync/health", base_url=base_url)


def put_object(key: str, data: bytes, content_type: str) -> None:
    call("PUT", f"/_sync/objects/{urllib.parse.quote(key)}", body=data, content_type=content_type)


def delete_object(key: str) -> None:
    call("DELETE", f"/_sync/objects/{urllib.parse.quote(key)}")


def records(upsert: list[dict], delete: list[dict]) -> None:
    call("POST", "/_sync/records", data={"upsert": upsert, "delete": delete})


def inbox(after: int, limit: int = 100) -> list[dict]:
    return call("GET", f"/_sync/inbox?after={after}&limit={limit}").get("events", [])


def ack(upto: int) -> None:
    call("POST", "/_sync/ack", data={"upto": upto})


def end_sessions(customer: str | None = None) -> None:
    call("POST", "/_sync/sessions/end", data={"customer": customer})


def purge() -> dict:
    return call("POST", "/_sync/purge", data={})
