"""The customer portal on Cloudflare over the API: how it's doing, its
settings, and syncing, deploying, stopping and removing it, as on its settings
page. The API token and the bot check's secret key can be set, never read."""

from __future__ import annotations

import datetime as dt

from django.core.exceptions import ValidationError
from django.db.models import Count
from ninja import Router, Schema, Status

from trammel.core import access, secrets

from . import client, cloudflare, deploy, sync
from .models import EdgeSettings, Published

router = Router(tags=["portal on Cloudflare"])

PERMISSION = "edge.manage"


class StepOut(Schema):
    at: str
    text: str


class EdgeStatusOut(Schema):
    #: Serving customers (Trammel's own portal pages are off).
    active: bool
    #: Deployed, waiting for its new address to answer before going live.
    awaiting_address: bool
    deploying: bool
    token_set: bool
    public_url: str
    #: Where customers go: public_url + /portal/.
    portal_url: str
    hostname: str
    account_id: str
    account_name: str
    deployed_version: str
    deployed_at: dt.datetime | None
    last_sync_at: dt.datetime | None
    last_error: str
    last_error_at: dt.datetime | None
    bot_check: bool
    #: A bot check made by hand in Cloudflare's dashboard (account API tokens).
    turnstile_own_sitekey: str
    turnstile_secret_set: bool
    keep_closed_days: int
    #: What's published there, by type: {"job": 2, "quote": 3, ...}.
    published: dict[str, int]
    #: The last of the portal's inbox events applied here.
    inbox_after: int
    #: Inbox events that failed, by number: how many times so far (skipped at 5).
    failures: dict[str, int]
    #: The last deploy's steps.
    deploy_log: list[StepOut]


class SyncOut(Schema):
    pushed: int = 0
    removed: int = 0
    applied: int = 0
    error: str = ""


def _status() -> dict:
    row = EdgeSettings.load()
    counts = Published.objects.values("type").annotate(n=Count("id"))
    return {
        "active": row.active,
        "awaiting_address": row.awaiting_address,
        "deploying": row.deploying,
        "token_set": secrets.is_set(deploy.TOKEN_KEY),
        "public_url": row.public_url,
        "portal_url": f"{row.public_url}/portal/" if row.public_url else "",
        "hostname": row.hostname,
        "account_id": row.account_id,
        "account_name": row.account_name,
        "deployed_version": row.deployed_version,
        "deployed_at": row.deployed_at,
        "last_sync_at": row.last_sync_at,
        "last_error": row.last_error,
        "last_error_at": row.last_error_at,
        "bot_check": bool(row.turnstile_sitekey),
        "turnstile_own_sitekey": row.turnstile_own_sitekey,
        "turnstile_secret_set": secrets.is_set(deploy.TURNSTILE_OWN_KEY),
        "keep_closed_days": row.keep_closed_days,
        "published": {c["type"]: c["n"] for c in counts},
        "inbox_after": row.inbox_after,
        "failures": {str(k): v for k, v in (row.failures or {}).items()},
        "deploy_log": [
            {"at": s.get("at", ""), "text": s.get("text", "")} for s in row.deploy_log or []
        ],
    }


class EdgeSettingsIn(Schema):
    """Only what's given changes. Secrets are write-only."""

    account_id: str | None = None
    #: Such as portal.example.com; "" for the account's workers.dev address.
    hostname: str | None = None
    keep_closed_days: int | None = None
    turnstile_own_sitekey: str | None = None
    api_token: str | None = None
    turnstile_secret: str | None = None


@router.get("/status", response=EdgeStatusOut)
def status(request):
    access.require(request.actor, PERMISSION)
    return _status()


@router.patch("/settings", response=EdgeStatusOut)
def update_settings(request, payload: EdgeSettingsIn):
    """Checked as on the settings page. Deploy for the changes to reach
    Cloudflare."""
    from .views import EdgeForm

    access.require(request.actor, PERMISSION)
    row = EdgeSettings.load()
    data = {
        "account_id": row.account_id,
        "hostname": row.hostname,
        "keep_closed_days": row.keep_closed_days,
        "turnstile_own_sitekey": row.turnstile_own_sitekey,
        "api_token": "",
        "turnstile_secret": "",
    }
    data.update({k: v for k, v in payload.dict(exclude_unset=True).items() if v is not None})
    form = EdgeForm(data, instance=row)
    if not form.is_valid():
        raise ValidationError({field: list(errors) for field, errors in form.errors.items()})
    form.save()
    return _status()


@router.post("/sync", response=SyncOut)
def sync_now(request):
    """One sync now: publish what changed, apply what customers did. A failure
    is in `error` (and on the settings page), not an error response."""
    access.require(request.actor, PERMISSION)
    if not EdgeSettings.load().active:
        raise ValidationError("The portal on Cloudflare isn't deployed.")
    return sync.run()


@router.post("/deploy", response={202: EdgeStatusOut})
def start_deploy(request):
    """Deploy, or update, in the background; follow it in /status (`deploying`,
    `deploy_log`)."""
    access.require(request.actor, PERMISSION)
    if not secrets.is_set(deploy.TOKEN_KEY):
        raise ValidationError("Paste a Cloudflare API token on the settings page first.")
    if not deploy.start():
        raise ValidationError("Already deploying.")
    return Status(202, _status())


@router.post("/stop", response=EdgeStatusOut)
def stop(request):
    """Remove everything published there and sign customers out; Trammel's own
    portal pages come back. The Worker, database and storage stay, empty."""
    access.require(request.actor, PERMISSION)
    deploy.stop()
    return _status()


@router.post("/remove", response=EdgeStatusOut)
def remove(request):
    """Delete the Worker, its database, its storage and the bot check it set up
    from the Cloudflare account. Deploying again starts afresh."""
    access.require(request.actor, PERMISSION)
    try:
        deploy.remove()
    except (deploy.DeployError, cloudflare.CloudflareError, client.EdgeError) as exc:
        raise ValidationError(str(exc)) from None
    return _status()
