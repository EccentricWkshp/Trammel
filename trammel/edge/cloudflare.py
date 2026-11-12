"""Cloudflare's API, for deploying the edge portal from Trammel (0.9.x): the
Worker, its D1 database and R2 bucket, its address, its schedule. Outbound
HTTPS only, with the API token the admin pasted in."""

from __future__ import annotations

import json
import secrets as random
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

API = "https://api.cloudflare.com/client/v4"
TIMEOUT = 60
#: Workers features this deployment is written for.
COMPATIBILITY_DATE = "2026-06-01"

#: What the token needs, as Cloudflare's token page names them.
PERMISSIONS = (
    ("Account", "Workers Scripts", "Edit"),
    ("Account", "D1", "Edit"),
    ("Account", "Workers R2 Storage", "Edit"),
    ("Account", "Account Settings", "Read"),
    ("Account", "Turnstile", "Edit", "for the bot check on signing in; user tokens only"),
    ("Zone", "Workers Routes", "Edit", "for a custom address"),
    ("Zone", "Zone", "Read", "for a custom address"),
)


class CloudflareError(Exception):
    """Cloudflare refused or couldn't be reached; the message says what to do."""


def _explain(errors) -> str:
    parts = []
    for error in errors or []:
        code, message = error.get("code"), error.get("message", "")
        if code == 10042 or "enable R2" in message:
            return (
                "R2 isn't turned on for this Cloudflare account yet. Open R2 in the "
                "Cloudflare dashboard once and turn it on (Cloudflare asks for a "
                "payment method first, even for the free allowance), then deploy again."
            )
        parts.append(f"{message} ({code})" if code else message)
    return "; ".join(parts) or "Cloudflare refused the request."


def call(token, method, path, *, data=None, query=None, body=None, content_type=None):
    """One API call; its `result`. Raises CloudflareError."""
    from trammel.core.models import SystemSettings

    if SystemSettings.load().is_offline:
        raise CloudflareError("Outbound traffic is switched off in System settings.")
    url = API + path + (f"?{urllib.parse.urlencode(query)}" if query else "")
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": f"Trammel/{settings.APP_VERSION}",
    }
    if data is not None:
        body = json.dumps(data).encode()
        content_type = "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        if exc.code in (401, 403) and not payload.get("errors"):
            raise CloudflareError(
                "Cloudflare didn't accept the API token, or it lacks a permission."
            ) from None
        error = CloudflareError(_explain(payload.get("errors")))
        error.status = exc.code
        raise error from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudflareError(f"Couldn't reach Cloudflare: {getattr(exc, 'reason', exc)}") from None
    except ValueError:
        raise CloudflareError("Cloudflare sent back something that isn't JSON.") from None
    if not payload.get("success", True):
        raise CloudflareError(_explain(payload.get("errors")))
    return payload.get("result")


def accounts(token) -> list[dict]:
    """The accounts the token can reach: [{id, name}]."""
    return [{"id": a["id"], "name": a.get("name", "")} for a in call(token, "GET", "/accounts")]


def account(token, account_id) -> dict:
    """One account, by its id: {id, name}. Works for an account API token,
    which is made in (and can reach) only its own account."""
    found = call(token, "GET", f"/accounts/{account_id}") or {}
    return {"id": found.get("id") or account_id, "name": found.get("name", "")}


def database(token, account, name) -> str:
    """The D1 database's id, creating it the first time."""
    found = call(token, "GET", f"/accounts/{account}/d1/database", query={"name": name}) or []
    for row in found:
        if row.get("name") == name:
            return row["uuid"]
    return call(token, "POST", f"/accounts/{account}/d1/database", data={"name": name})["uuid"]


def bucket(token, account, name) -> str:
    """The R2 bucket, creating it the first time."""
    try:
        call(token, "GET", f"/accounts/{account}/r2/buckets/{name}")
    except CloudflareError as exc:
        if getattr(exc, "status", None) != 404:
            raise
        call(token, "POST", f"/accounts/{account}/r2/buckets", data={"name": name})
    return name


def _multipart(parts) -> tuple[bytes, str]:
    """parts: (name, filename or None, content type, bytes)."""
    boundary = f"trammel-{random.token_hex(12)}"
    out = []
    for name, filename, ctype, data in parts:
        disposition = f'form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        out.append(
            f"--{boundary}\r\nContent-Disposition: {disposition}\r\n"
            f"Content-Type: {ctype}\r\n\r\n".encode()
            + data
            + b"\r\n"
        )
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def upload_worker(token, account, name, modules: dict[str, bytes], bindings: list[dict]):
    """Upload the Worker's modules (the first is the entry point) with their
    bindings; Cloudflare deploys it at once."""
    main = next(iter(modules))
    metadata = {
        "main_module": main,
        "compatibility_date": COMPATIBILITY_DATE,
        "bindings": bindings,
    }
    parts = [("metadata", None, "application/json", json.dumps(metadata).encode())]
    parts += [
        (filename, filename, "application/javascript+module", source)
        for filename, source in modules.items()
    ]
    body, ctype = _multipart(parts)
    return call(
        token, "PUT", f"/accounts/{account}/workers/scripts/{name}", body=body, content_type=ctype
    )


def schedules(token, account, name, crons: list[str]):
    return call(
        token,
        "PUT",
        f"/accounts/{account}/workers/scripts/{name}/schedules",
        data=[{"cron": c} for c in crons],
    )


def workers_dev(token, account, name) -> str:
    """Put the Worker on the account's workers.dev address; returns its URL."""
    sub = (call(token, "GET", f"/accounts/{account}/workers/subdomain") or {}).get("subdomain")
    if not sub:
        raise CloudflareError(
            "This account has no workers.dev address yet. Open Workers & Pages in the "
            "Cloudflare dashboard once to choose one, or give a custom address instead."
        )
    call(
        token,
        "POST",
        f"/accounts/{account}/workers/scripts/{name}/subdomain",
        data={"enabled": True},
    )
    return f"https://{name}.{sub}.workers.dev"


def put_secret(token, account, name, key, value):
    """Set one of the Worker's secrets without uploading it again."""
    return call(
        token,
        "PUT",
        f"/accounts/{account}/workers/scripts/{name}/secrets",
        data={"name": key, "text": value, "type": "secret_text"},
    )


def turnstile(token, account, name, hostname, sitekey="") -> dict:
    """The bot check for the sign-in pages: the widget for `hostname`, made the
    first time ({sitekey, secret}) or kept up to date ({sitekey})."""
    body = {"name": name, "domains": [hostname], "mode": "managed"}
    if sitekey:
        try:
            call(token, "PUT", f"/accounts/{account}/challenges/widgets/{sitekey}", data=body)
            return {"sitekey": sitekey}
        except CloudflareError as exc:
            if getattr(exc, "status", None) != 404:
                raise
    made = call(token, "POST", f"/accounts/{account}/challenges/widgets", data=body)
    return {"sitekey": made["sitekey"], "secret": made["secret"]}


def remove(token, account, name, database_id="", bucket_name="", sitekey=""):
    """Take the Worker, its database, bucket (emptied first) and bot check off
    the account. What's already gone is passed over."""

    def gone(method, path):
        try:
            call(token, method, path)
        except CloudflareError as exc:
            if getattr(exc, "status", None) != 404:
                raise

    gone("DELETE", f"/accounts/{account}/workers/scripts/{name}?force=true")
    if database_id:
        gone("DELETE", f"/accounts/{account}/d1/database/{database_id}")
    if bucket_name:
        gone("DELETE", f"/accounts/{account}/r2/buckets/{bucket_name}")
    if sitekey:
        gone("DELETE", f"/accounts/{account}/challenges/widgets/{sitekey}")


#: Cloudflare's public resolver, asked over HTTPS, so the local one isn't.
PUBLIC_DNS = "https://cloudflare-dns.com/dns-query"


def in_public_dns(hostname) -> bool:
    """Whether a new address is in DNS yet, asked of Cloudflare's resolver
    directly. Asking the shop's own resolver too soon would have it remember
    "no such name" for as long as the domain says (30 minutes on Cloudflare)."""
    url = f"{PUBLIC_DNS}?{urllib.parse.urlencode({'name': hostname, 'type': 'A'})}"
    request = urllib.request.Request(  # noqa: S310
        url,
        headers={
            "Accept": "application/dns-json",
            "User-Agent": f"Trammel/{settings.APP_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            answer = json.loads(response.read() or b"{}")
    except urllib.error.URLError, TimeoutError, OSError, ValueError:
        return False
    return answer.get("Status") == 0 and bool(answer.get("Answer"))


def zone_for(token, hostname) -> dict:
    """The Cloudflare zone a hostname is in: its longest matching domain."""
    labels = hostname.lower().strip(".").split(".")
    for i in range(len(labels) - 1):
        candidate = ".".join(labels[i:])
        found = call(token, "GET", "/zones", query={"name": candidate}) or []
        if found:
            return {"id": found[0]["id"], "name": found[0]["name"]}
    raise CloudflareError(
        f"No domain on this Cloudflare account covers {hostname}. Its DNS must be on "
        "Cloudflare, or leave the custom address blank to use workers.dev."
    )


def custom_domain(token, account, name, hostname) -> str:
    """Serve the Worker at a hostname on one of the account's domains."""
    zone = zone_for(token, hostname)
    call(
        token,
        "PUT",
        f"/accounts/{account}/workers/domains",
        data={
            "hostname": hostname,
            "service": name,
            "zone_id": zone["id"],
            "zone_name": zone["name"],
            "enabled": True,
        },
    )
    return f"https://{hostname}"
