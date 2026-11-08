"""A minimal S3 client for the off-site backup copy (FR-BAK-1).

Just what backups need — put, list and delete objects — signed with AWS
Signature Version 4 and sent over the standard library, so there is no SDK to
install. Path-style URLs (`https://endpoint/bucket/key`) work with AWS S3,
Backblaze B2, Cloudflare R2, Wasabi and MinIO.

Uploads stream from disk with `UNSIGNED-PAYLOAD`, so a large backup is never
read into memory; TLS protects the body in transit. A single PUT is limited to
5 GB, which is far more than a small shop's backup.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

SERVICE = "s3"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
TIMEOUT = 600


class S3Error(Exception):
    pass


@dataclass
class Target:
    endpoint: str  # https://s3.us-west-2.amazonaws.com
    region: str
    bucket: str
    access_key: str
    secret_key: str
    prefix: str = ""

    def key(self, name: str) -> str:
        prefix = self.prefix.strip("/")
        return f"{prefix}/{name}" if prefix else name


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str, service: str = SERVICE) -> bytes:
    k = _hmac(("AWS4" + secret).encode(), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def _quote(value: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(value, safe=safe)


def sign(
    target: Target,
    method: str,
    key: str = "",
    query: dict[str, str] | None = None,
    *,
    payload_hash: str = EMPTY_SHA256,
    headers: dict[str, str] | None = None,
    now: dt.datetime | None = None,
) -> tuple[str, dict[str, str]]:
    """The request URL and the headers that sign it."""
    now = now or dt.datetime.now(dt.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    parts = urllib.parse.urlsplit(target.endpoint.rstrip("/"))
    host = parts.netloc
    # An empty bucket means the endpoint already names it (virtual-hosted style).
    path = parts.path + (f"/{_quote(target.bucket)}" if target.bucket else "")
    if key:
        path += "/" + _quote(key, safe="-_.~/")
    path = path or "/"
    query = query or {}
    canonical_query = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items()))
    all_headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        **{k.lower(): v for k, v in (headers or {}).items()},
    }
    names = sorted(all_headers)
    canonical_headers = "".join(f"{n}:{str(all_headers[n]).strip()}\n" for n in names)
    signed_headers = ";".join(names)
    canonical = "\n".join(
        [method, path, canonical_query, canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{date}/{target.region}/{SERVICE}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    signature = hmac.new(
        signing_key(target.secret_key, date, target.region), to_sign.encode(), hashlib.sha256
    ).hexdigest()
    all_headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={target.access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    del all_headers["host"]  # urllib sets it
    url = f"{parts.scheme}://{host}{path}" + (f"?{canonical_query}" if canonical_query else "")
    return url, all_headers


def _request(url: str, **kwargs) -> urllib.request.Request:
    if urllib.parse.urlsplit(url).scheme not in ("https", "http"):
        raise S3Error("The endpoint must be an https:// address.")
    return urllib.request.Request(url, **kwargs)  # noqa: S310 — scheme checked above


def _send(request: urllib.request.Request) -> bytes:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 — admin-set https endpoint
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        message = body
        try:
            root = ET.fromstring(body)  # noqa: S314 — the configured endpoint's error document
            message = root.findtext("Message") or root.findtext("Code") or body
        except ET.ParseError:
            pass
        raise S3Error(f"{exc.code} {exc.reason}: {message[:300]}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise S3Error(f"Could not reach {request.full_url.split('?')[0]}: {exc}") from exc


def put_file(target: Target, path: Path, name: str | None = None) -> str:
    key = target.key(name or path.name)
    size = path.stat().st_size
    url, headers = sign(
        target,
        "PUT",
        key,
        payload_hash="UNSIGNED-PAYLOAD",
        headers={"content-length": str(size), "content-type": "application/gzip"},
    )
    with path.open("rb") as body:
        _send(_request(url, data=body, headers=headers, method="PUT"))
    return key


def put_bytes(target: Target, name: str, data: bytes) -> str:
    key = target.key(name)
    url, headers = sign(
        target,
        "PUT",
        key,
        payload_hash=hashlib.sha256(data).hexdigest(),
        headers={"content-length": str(len(data))},
    )
    _send(_request(url, data=data, headers=headers, method="PUT"))
    return key


def list_keys(target: Target, prefix: str = "") -> list[str]:
    """Every key under the target's prefix (and `prefix` within it), oldest name first."""
    base = target.prefix.strip("/")
    full = f"{base}/{prefix}" if base else prefix
    keys: list[str] = []
    token = None
    while True:
        query = {"list-type": "2", "prefix": full}
        if token:
            query["continuation-token"] = token
        url, headers = sign(target, "GET", "", query)
        root = ET.fromstring(_send(_request(url, headers=headers)))  # noqa: S314
        keys += [el.text for el in root.iter(f"{NS}Key") if el.text]
        keys += [el.text for el in root.iter("Key") if el.text]  # services without the namespace
        if (root.findtext(f"{NS}IsTruncated") or root.findtext("IsTruncated")) != "true":
            break
        token = root.findtext(f"{NS}NextContinuationToken") or root.findtext(
            "NextContinuationToken"
        )
        if not token:
            break
    return sorted(set(keys))


def delete(target: Target, key: str) -> None:
    url, headers = sign(target, "DELETE", key)
    _send(_request(url, headers=headers, method="DELETE"))
