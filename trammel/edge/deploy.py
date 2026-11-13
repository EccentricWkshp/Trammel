"""Deploying the edge portal to Cloudflare from Trammel (0.9.x), over the API:
the database and bucket, the Worker with its bindings and secrets, its
schedule and address; then a check that it answers, and the first sync. Run
as a background task; each step is logged for the settings page."""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

from django.conf import settings as django_settings
from django.utils import timezone

from trammel.core import audit, secrets

from . import client, cloudflare
from .models import EdgeSettings

TOKEN_KEY = "edge.api_token"
TURNSTILE_KEY = "edge.turnstile_secret"
#: The secret key of a bot check made by hand (turnstile_own_sitekey).
TURNSTILE_OWN_KEY = "edge.turnstile_own_secret"
WORKER_DIR = Path(__file__).parent / "worker"
EBAY_TOKEN_KEY = "edge.ebay_token"
#: The entry point first: Cloudflare runs the first module.
MODULES = (
    "worker.js",
    "auth.js",
    "crypto.js",
    "db.js",
    "hooks.js",
    "html.js",
    "portal.js",
    "respond.js",
    "send.js",
    "smtp.js",
    "sync.js",
)


class DeployError(Exception):
    pass


def ebay_token() -> str:
    """The verification token eBay is given with the deletion notices address,
    made the first time it's wanted (eBay wants 32 to 80 letters and digits)."""
    import secrets as random

    token = secrets.get(EBAY_TOKEN_KEY)
    if not token:
        token = random.token_hex(24)
        secrets.put(EBAY_TOKEN_KEY, token)
    return token


def shopify_hook_secret() -> str:
    """What the Worker checks Shopify's webhooks with before keeping them
    (SEC-08, 0.10.6): Shopify's webhook secret while its webhooks are on, or
    "none" (Cloudflare keeps no empty secret)."""
    from trammel.core import modules

    if not modules.is_enabled("imports"):
        return "none"
    from trammel.imports import shopify
    from trammel.imports.models import ShopifySettings

    shop = ShopifySettings.load()
    secret = shopify.webhook_secret(shop) if shop.enabled and shop.webhooks_enabled else ""
    return secret or "none"


def _hook_hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(f"trammel-shopify-hook:{value}".encode()).hexdigest()


def push_hook_secret() -> bool:
    """Give the Worker Shopify's webhook secret again when it has changed since
    it was last given (run with each sync). Whether it was sent."""
    row = EdgeSettings.load()
    value = shopify_hook_secret()
    digest = _hook_hash(value)
    token = secrets.get(TOKEN_KEY)
    if row.hook_secret_hash == digest or not (token and row.account_id and row.active):
        return False
    cloudflare.put_secret(token, row.account_id, row.worker_name, "SHOPIFY_WEBHOOK_SECRET", value)
    EdgeSettings.objects.filter(pk=1).update(hook_secret_hash=digest)
    return True


def worker_modules() -> dict[str, bytes]:
    return {name: (WORKER_DIR / name).read_bytes() for name in MODULES}


def smtp_settings() -> dict:
    """The shop's email settings for the Worker, which sends sign-in codes. Only
    encrypted SMTP on 465 or 587 (Cloudflare blocks 25; mail should be
    encrypted anyway)."""
    from trammel.notifications import services as notifications
    from trammel.notifications.models import SmtpSettings

    smtp = SmtpSettings.load()
    if not smtp.host or not smtp.from_address:
        raise DeployError(
            "Set up email first (Settings, Email): the edge portal sends sign-in codes."
        )
    if smtp.security not in ("ssl", "starttls") or smtp.port not in (465, 587):
        raise DeployError(
            "The edge portal needs encrypted email: SSL/TLS on port 465 or STARTTLS on "
            "port 587 (Settings, Email). Cloudflare doesn't allow port 25."
        )
    return {
        "host": smtp.host,
        "port": smtp.port,
        "security": smtp.security,
        "username": smtp.username,
        "password": secrets.get(notifications.PASSWORD_KEY) or "",
        "from_address": smtp.from_address,
        "from_name": smtp.from_name,
    }


def _log(text: str) -> None:
    row = EdgeSettings.load()
    row.deploy_log = [*row.deploy_log, {"at": timezone.now().isoformat(), "text": text}]
    row.save(update_fields=["deploy_log", "updated_at"])


def deploy() -> None:
    """Deploy or update the edge portal. Raises DeployError (logged) on failure."""
    row = EdgeSettings.load()
    token = secrets.get(TOKEN_KEY)
    if not token:
        raise DeployError("Paste a Cloudflare API token first.")
    smtp = smtp_settings()

    _log("Checking the API token")
    account = _account(token, row)
    row.account_id, row.account_name = account["id"], account["name"]
    row.save(update_fields=["account_id", "account_name", "updated_at"])

    _log("Making the database (D1)")
    database_id = cloudflare.database(token, account["id"], row.worker_name)
    _log("Making the file storage (R2)")
    bucket = cloudflare.bucket(token, account["id"], row.worker_name)

    _log("Uploading the portal (Worker)")
    cloudflare.upload_worker(
        token,
        account["id"],
        row.worker_name,
        worker_modules(),
        [
            {"type": "d1", "name": "DB", "id": database_id},
            {"type": "r2_bucket", "name": "FILES", "bucket_name": bucket},
            {"type": "secret_text", "name": "SYNC_KEY", "text": client.sync_key()},
            {"type": "secret_text", "name": "SMTP", "text": json.dumps(smtp)},
            {"type": "secret_text", "name": "EBAY_TOKEN", "text": ebay_token()},
            {
                "type": "secret_text",
                "name": "SHOPIFY_WEBHOOK_SECRET",
                "text": shopify_hook_secret(),
            },
            {
                "type": "secret_text",
                "name": "TURNSTILE_SECRET",
                "text": _turnstile_secret(row),
            },
            {"type": "plain_text", "name": "TRAMMEL_VERSION", "text": django_settings.APP_VERSION},
        ],
    )
    _log("Scheduling its cleanup")
    cloudflare.schedules(token, account["id"], row.worker_name, ["*/30 * * * *"])

    if row.hostname:
        _log(f"Serving it at {row.hostname}")
        url = cloudflare.custom_domain(token, account["id"], row.worker_name, row.hostname)
    else:
        _log("Serving it at its workers.dev address")
        url = cloudflare.workers_dev(token, account["id"], row.worker_name)

    _bot_check(token, account["id"], row, url)
    EdgeSettings.objects.filter(pk=1).update(
        hook_secret_hash=_hook_hash(shopify_hook_secret()),
        database_id=database_id,
        bucket=bucket,
        public_url=url,
        deployed_version=django_settings.APP_VERSION,
        deployed_at=timezone.now(),
    )
    if row.hostname:
        _wait_for_dns(row.hostname)

    _log(f"Checking that {url} answers")
    for wait in (0, 3, 5, 10, 15, 20):
        time.sleep(wait)
        try:
            client.health(base_url=url)
            break
        except client.EdgeError:
            continue
    else:
        EdgeSettings.objects.filter(pk=1).update(awaiting_address=True)
        _log(
            f"Deployed, but {url} doesn't answer from here yet. A new address can take up to "
            "30 minutes to reach every DNS server; Trammel keeps checking and puts the portal "
            "live by itself when it answers."
        )
        return
    _go_live(url)


def _wait_for_dns(hostname) -> None:
    """Wait, up to two minutes, for a new address to be in Cloudflare's DNS
    before anything here looks it up (see cloudflare.in_public_dns)."""
    _log(f"Waiting for {hostname} to be in DNS")
    for wait in (0, 5, 10, 15, 20, 30, 40):
        time.sleep(wait)
        if cloudflare.in_public_dns(hostname):
            return
    _log(f"{hostname} isn't in DNS yet; checking anyway")


def _go_live(url) -> None:
    """The Worker answers: serve customers from it, and publish to it."""
    EdgeSettings.objects.filter(pk=1).update(active=True, awaiting_address=False, last_error="")
    _log("Publishing to it")
    from . import sync

    result = sync.run()
    if result.get("error"):
        raise DeployError(f"Deployed, but the first sync failed: {result['error']}")
    _log(f"Done: the customer portal is at {url}/portal/")


def finish_waiting() -> None:
    """Run with each sync: a deploy whose address didn't answer yet goes live
    once it does."""
    row = EdgeSettings.load()
    if not row.awaiting_address or row.active or row.deploying or not row.public_url:
        return
    try:
        client.health(base_url=row.public_url)
    except client.EdgeError:
        return
    _log(f"{row.public_url} answers now")
    try:
        _go_live(row.public_url)
    except (DeployError, client.EdgeError) as exc:
        _log(f"Stopped: {exc}")


def _account(token, row) -> dict:
    """The Cloudflare account to deploy to. With its id given, that account (an
    account API token reaches only its own, and may not list it); otherwise the
    one account the token can list."""
    if row.account_id:
        try:
            return cloudflare.account(token, row.account_id)
        except cloudflare.CloudflareError as exc:
            if getattr(exc, "status", None) in (403, 404):
                raise DeployError(
                    "The token can't reach the account whose ID is given. Check the ID, "
                    "and that the token was made in that account or can reach it."
                ) from None
            raise
    try:
        accounts = cloudflare.accounts(token)
    except cloudflare.CloudflareError as exc:
        raise DeployError(
            f"Couldn't list the token's accounts ({exc}). Enter the Cloudflare account ID "
            "on this page and deploy again."
        ) from None
    if len(accounts) == 1:
        return accounts[0]
    if not accounts:
        raise DeployError(
            "The token can't list any Cloudflare account. Enter the account ID on this page "
            "and deploy again."
        )
    raise DeployError("The token can reach several accounts; enter the ID of the one to use.")


def _turnstile_secret(row) -> str:
    if row.turnstile_own_sitekey:
        return secrets.get(TURNSTILE_OWN_KEY) or ""
    return secrets.get(TURNSTILE_KEY) or ""


def _bot_check(token, account, row, url) -> None:
    """Cloudflare Turnstile on the sign-in pages: the one whose keys were pasted
    in, or one made over the API. Without either (an account API token can't
    make one), the portal goes ahead without it (signing in is rate-limited
    anyway); the log says so."""
    from urllib.parse import urlparse

    if row.turnstile_own_sitekey:
        if not secrets.get(TURNSTILE_OWN_KEY):
            _log("Going ahead without the bot check: paste its secret key on this page too.")
            EdgeSettings.objects.filter(pk=1).update(turnstile_sitekey="")
            return
        _log("Using the bot check whose keys are on this page (Turnstile)")
        EdgeSettings.objects.filter(pk=1).update(turnstile_sitekey=row.turnstile_own_sitekey)
        return
    _log("Setting up the bot check on signing in (Turnstile)")
    try:
        widget = cloudflare.turnstile(
            token, account, row.worker_name, urlparse(url).hostname, row.turnstile_sitekey
        )
    except cloudflare.CloudflareError as exc:
        _log(
            f"Going ahead without the bot check: {exc}. An account API token can't set one "
            "up; to have one, make a Turnstile widget in Cloudflare's dashboard and paste "
            "its keys on this page."
        )
        EdgeSettings.objects.filter(pk=1).update(turnstile_sitekey="")
        return
    if widget.get("secret"):
        secrets.put(TURNSTILE_KEY, widget["secret"])
        cloudflare.put_secret(token, account, row.worker_name, "TURNSTILE_SECRET", widget["secret"])
    EdgeSettings.objects.filter(pk=1).update(turnstile_sitekey=widget["sitekey"])


def start() -> bool:
    """Start deploying in the background (the edge.deploy task). False when a
    deploy is already under way."""
    from trammel.core import tasks

    if EdgeSettings.load().deploying:
        return False
    EdgeSettings.objects.filter(pk=1).update(deploying=True, deploy_log=[])
    tasks.enqueue("edge.deploy", dedupe=True)
    audit.record("edge.deploy", summary="Started deploying the customer portal to Cloudflare")
    return True


def run_deploy() -> None:
    """The background task: deploy, logging how it went."""
    EdgeSettings.load()  # the row exists before it's updated
    EdgeSettings.objects.filter(pk=1).update(deploying=True, deploy_log=[], awaiting_address=False)
    try:
        deploy()
    except (DeployError, cloudflare.CloudflareError, client.EdgeError) as exc:
        _log(f"Stopped: {exc}")
    finally:
        EdgeSettings.objects.filter(pk=1).update(deploying=False)


def stop() -> None:
    """Stop using the edge portal: everything published there is removed and
    customers signed out; Trammel's own portal pages come back. The Worker,
    database and bucket stay on Cloudflare, empty, for next time."""
    row = EdgeSettings.load()
    if row.public_url:
        with contextlib.suppress(client.EdgeError):
            client.purge()
    from .models import Published

    Published.objects.all().delete()
    EdgeSettings.objects.filter(pk=1).update(active=False, awaiting_address=False)
    audit.record(
        "edge.stop",
        summary="Stopped serving the customer portal from Cloudflare; what was published "
        "there is removed",
    )


def remove() -> None:
    """Take everything off Cloudflare: what's published, then the Worker, its
    database, its bucket and its bot check. Deploying again starts afresh."""
    row = EdgeSettings.load()
    token = secrets.get(TOKEN_KEY)
    if not (token and row.account_id):
        raise DeployError("There's nothing on Cloudflare to remove.")
    stop()
    cloudflare.remove(
        token,
        row.account_id,
        row.worker_name,
        database_id=row.database_id,
        bucket_name=row.bucket,
        # A bot check made by hand stays: it isn't Trammel's to delete.
        sitekey="" if row.turnstile_sitekey == row.turnstile_own_sitekey else row.turnstile_sitekey,
    )
    secrets.clear(TURNSTILE_KEY)
    audit.record(
        "edge.remove",
        summary="Removed the customer portal's Worker, database, storage and bot check "
        "from Cloudflare",
    )
    EdgeSettings.objects.filter(pk=1).update(
        database_id="",
        bucket="",
        public_url="",
        deployed_version="",
        deployed_at=None,
        instance="",
        inbox_after=0,
        failures={},
        turnstile_sitekey="",
        deploy_log=[],
    )
