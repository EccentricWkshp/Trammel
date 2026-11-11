"""HTTPS calls to an online store, shared by the stores that sign in with OAuth."""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

TIMEOUT = 30


class StoreError(Exception):
    """Something went wrong talking to a store; the message says what to do."""


class HttpProblem(Exception):
    """The store answered with an error status. `detail` is its own explanation."""

    def __init__(self, status: int, detail: str, error: str = ""):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail, self.error = status, detail, error


def _explain(body: bytes) -> tuple[str, str]:
    """(error code, readable detail) from a store's error body, as far as it says."""
    with contextlib.suppress(ValueError, AttributeError):
        data = json.loads(body or b"{}")
        if isinstance(data, dict):
            error = str(data.get("error") or "")
            detail = data.get("error_description") or data.get("errors") or data.get("error")
            if isinstance(detail, (list, dict)):
                detail = json.dumps(detail)[:300]
            return error, str(detail or "")
    return "", ""


def call(label: str, method: str, url: str, *, data: dict | None = None, headers=None):
    """One request. Returns (parsed JSON, response headers). Raises StoreError when
    the store can't be reached (or outbound traffic is off), HttpProblem when it
    answers with an error."""
    from trammel.core.models import SystemSettings

    if SystemSettings.load().is_offline:
        raise StoreError("Outbound traffic is switched off in System settings.")
    if not url.startswith("https://"):
        raise StoreError(f"Refusing to talk to {label} over anything but HTTPS.")
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(  # noqa: S310 — https only, checked above
        url,
        data=body,
        method=method,
        headers={"User-Agent": f"Trammel/{settings.APP_VERSION}", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return json.loads(response.read() or b"{}"), response.headers
    except urllib.error.HTTPError as exc:
        error, detail = _explain(exc.read())
        raise HttpProblem(exc.code, detail or str(exc.reason), error) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise StoreError(f"Couldn't reach {label}: {getattr(exc, 'reason', exc)}") from None
    except ValueError:
        raise StoreError(f"{label} sent back something that isn't JSON.") from None
