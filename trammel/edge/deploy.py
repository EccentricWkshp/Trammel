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

from trammel.core import secrets

from . import client, cloudflare
from .models import EdgeSettings

TOKEN_KEY = "edge.api_token"
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
    accounts = cloudflare.accounts(token)
    if row.account_id:
        account = next((a for a in accounts if a["id"] == row.account_id), None)
        if account is None:
            raise DeployError("The token can't reach the account chosen.")
    elif len(accounts) == 1:
        account = accounts[0]
    elif not accounts:
        raise DeployError("The token can't reach any Cloudflare account.")
    else:
        raise DeployError("The token can reach several accounts; choose one.")
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

    _log(f"Checking that {url} answers")
    health = None
    for wait in (0, 3, 5, 10, 15, 20):
        time.sleep(wait)
        try:
            health = client.health(base_url=url)
            break
        except client.EdgeError:
            continue
    if not health:
        raise DeployError(
            f"Deployed, but {url} doesn't answer yet (a new address can take a few minutes). "
            "Deploy again shortly."
        )
    EdgeSettings.objects.filter(pk=1).update(
        database_id=database_id,
        bucket=bucket,
        public_url=url,
        deployed_version=django_settings.APP_VERSION,
        deployed_at=timezone.now(),
        active=True,
        last_error="",
    )
    _log("Publishing to it")
    from . import sync

    result = sync.run()
    if result.get("error"):
        raise DeployError(f"Deployed, but the first sync failed: {result['error']}")
    _log(f"Done: the customer portal is at {url}/portal/")


def run_deploy() -> None:
    """The background task: deploy, logging how it went."""
    EdgeSettings.load()  # the row exists before it's updated
    EdgeSettings.objects.filter(pk=1).update(deploying=True, deploy_log=[])
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
    EdgeSettings.objects.filter(pk=1).update(active=False)
