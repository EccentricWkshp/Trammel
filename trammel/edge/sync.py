"""The sync with the edge portal (0.9.x): push what changed, then pull what
customers did and apply it here, through the same services as Trammel's own
portal. Run by the background worker every 30 seconds, and on demand."""

from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.utils import timezone

from trammel.core import access, audit

from . import client, publish
from .models import EdgeSettings, Published

log = logging.getLogger(__name__)
BATCH = 100


def push() -> dict:
    """Publish what changed and take down what's no longer shown. Each item's
    files are stored before its record, and old files removed after, so a page
    never points at something missing."""
    stored = {(p.type, p.key): p for p in Published.objects.all()}
    seen: set = set()
    pending: list[tuple] = []  # (item, record, objects stored, stale objects)
    counts = {"pushed": 0, "removed": 0}

    def flush():
        if not pending:
            return
        client.records([record for _, record, _, _ in pending], [])
        for item, _, objects, stale in pending:
            Published.objects.update_or_create(
                type=item.type,
                key=item.id,
                defaults={"fingerprint": item.fingerprint, "objects_stored": objects},
            )
            for key in stale:
                client.delete_object(key)
        counts["pushed"] += len(pending)
        pending.clear()

    for item in publish.items():
        key = (item.type, item.id)
        seen.add(key)
        before = stored.get(key)
        if before is not None and before.fingerprint == item.fingerprint:
            continue
        data, objects = item.build()
        for object_key, (content, content_type) in objects.items():
            client.put_object(object_key, content, content_type)
        record = {
            "type": item.type,
            "id": item.id,
            "customer": item.customer,
            "job": item.job,
            "sort": item.sort,
            "data": data,
        }
        stale = [k for k in (before.objects_stored if before else []) if k not in objects]
        pending.append((item, record, list(objects), stale))
        if len(pending) >= BATCH:
            flush()
    flush()

    gone = [p for key, p in stored.items() if key not in seen]
    for start in range(0, len(gone), BATCH):
        chunk = gone[start : start + BATCH]
        client.records([], [{"type": p.type, "id": p.key} for p in chunk])
        for p in chunk:
            for key in p.objects_stored:
                client.delete_object(key)
            p.delete()
        counts["removed"] += len(chunk)
    return counts


# ---------------------------------------------------------------------------
# The inbox: what customers did
# ---------------------------------------------------------------------------


def _customer(event):
    from trammel.customers.models import Customer

    return Customer.objects.filter(pk=event.get("customer") or None).first()


def _at(event) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
    except KeyError, ValueError:
        return timezone.now()


def _code_sent(event) -> None:
    from trammel.notifications.models import EmailLog

    data = event["data"]
    customer = _customer({"customer": data.get("customer")})
    EmailLog.objects.create(
        event="portal_code",
        to=data.get("email", ""),
        subject="Portal sign-in code",
        status=EmailLog.Status.SENT,
        detail="Sent by the edge portal on Cloudflare.",
        at=_at(event),
    )
    if customer:
        audit.record(
            "portal.code_sent",
            customer,
            summary=f"Sent a portal sign-in code to {data.get('email', '')} (edge portal)",
            actor=access.ANONYMOUS,
        )


def _mail_failed(event) -> None:
    from trammel.notifications.models import EmailLog

    data = event["data"]
    EmailLog.objects.create(
        event="portal_code",
        to=data.get("email", ""),
        subject="Portal sign-in code",
        status=EmailLog.Status.FAILED,
        detail=f"The edge portal couldn't send it: {data.get('error', '')}"[:2000],
        at=_at(event),
    )


def _signed_in(event) -> None:
    from trammel.portal import services as portal
    from trammel.portal.models import Access

    data = event["data"]
    customer = _customer(data)
    if customer is None:
        return
    email = data.get("email", "")
    name = portal.name_for(customer, email) if email else customer.name
    Access.objects.filter(customer=customer).update(last_sign_in_at=_at(event))
    audit.record(
        "portal.sign_in",
        customer,
        summary=f"{name} ({email}) signed in to the portal (edge portal, "
        f"from {event.get('ip') or 'an unknown address'})",
        actor=portal._actor(customer, name),
    )


def _access_code_used(event) -> None:
    from trammel.portal import services as portal
    from trammel.portal.models import Access, AccessCode

    data = event["data"]
    customer = _customer(data)
    if customer is None:
        return
    code = AccessCode.objects.filter(pk=data.get("code") or None, customer=customer).first()
    given_by = "staff"
    if code is not None:
        AccessCode.objects.filter(pk=code.pk, used_at=None).update(used_at=_at(event))
        given_by = code.created_by.display_name if code.created_by else "staff"
    Access.objects.filter(customer=customer).update(last_sign_in_at=_at(event))
    audit.record(
        "portal.sign_in",
        customer,
        summary=f"{customer} signed in to the portal with a code from {given_by} (edge portal)",
        actor=portal._actor(customer, customer.name),
    )


HANDLERS = {
    "code_sent": _code_sent,
    "mail_failed": _mail_failed,
    "signed_in": _signed_in,
    "access_code_used": _access_code_used,
}


def pull() -> int:
    """Apply the inbox in order, each event once, then let the Worker drop what's
    been applied. Returns how many were applied."""
    settings = EdgeSettings.load()
    applied = 0
    while True:
        events = client.inbox(settings.inbox_after)
        if not events:
            break
        for event in events:
            handler = HANDLERS.get(event["kind"])
            with transaction.atomic():
                if handler is not None:
                    handler(event)
                else:
                    log.warning("Edge inbox: no handler for %s", event["kind"])
                EdgeSettings.objects.filter(pk=1).update(inbox_after=event["seq"])
            settings.inbox_after = event["seq"]
            applied += 1
        client.ack(settings.inbox_after)
    return applied


def run() -> dict:
    """One sync, if the edge portal is deployed: push, then pull. Problems are
    kept on the settings page rather than raised."""
    settings = EdgeSettings.load()
    if not settings.active:
        return {}
    try:
        counts = push()
        counts["applied"] = pull()
    except Exception as exc:  # noqa: BLE001 — shown on the settings page, retried next time
        EdgeSettings.objects.filter(pk=1).update(
            last_error=str(exc)[:2000], last_error_at=timezone.now()
        )
        log.warning("Edge portal sync failed: %s", exc)
        return {"error": str(exc)}
    EdgeSettings.objects.filter(pk=1).update(last_sync_at=timezone.now(), last_error="")
    return counts
