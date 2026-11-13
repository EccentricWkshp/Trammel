"""The sync with the edge portal (0.9.x): push what changed, then pull what
customers did and apply it here, through the same services as Trammel's own
portal. Run by the background worker every 30 seconds, and on demand."""

from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.utils import timezone

from trammel.core import access, audit, modules

from . import client, publish
from .models import EdgeSettings, Published

log = logging.getLogger(__name__)
BATCH = 100


def _published_as(item) -> str:
    """What decides whether a record goes out again: its content and also whose
    it is (customer, job) and its place in lists. The Worker lets a customer see
    a record by its customer, so a job moved to another customer must republish
    even when nothing in it changed (SEC-01, 0.10.5). Changing this formula
    republishes everything once, which also repairs records published before."""
    return publish.fingerprint("v2", item.fingerprint, item.customer, item.job, item.sort)


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
                defaults={"fingerprint": _published_as(item), "objects_stored": objects},
            )
            for key in stale:
                client.delete_object(key)
        counts["pushed"] += len(pending)
        pending.clear()

    for item in publish.items():
        key = (item.type, item.id)
        seen.add(key)
        before = stored.get(key)
        if before is not None and before.fingerprint == _published_as(item):
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


def _quote_response(event) -> None:
    """An answer to a quote (FR-PRT-3): applied through the quote services, as
    Trammel's own portal does; one that can't be is kept for the customer."""
    from django.core.exceptions import PermissionDenied, ValidationError

    from trammel.agreements.models import AgreementSettings
    from trammel.core import signature as signatures
    from trammel.core.lifecycle import TransitionError
    from trammel.portal import services as portal
    from trammel.quotes import services as quotes
    from trammel.quotes.models import QuoteRevision

    from .models import EdgeAnswer

    data = event["data"]
    customer = _customer(event)
    revision = (
        QuoteRevision.objects.filter(
            pk=data.get("revision") or None, quote__job__customer=customer
        ).first()
        if customer
        else None
    )
    if revision is None:
        return
    noun = revision.quote.noun
    name = (data.get("name") or "").strip()[:150]
    actor = portal._actor(customer, name or customer.name)
    comment = (data.get("comment") or "").strip()[:2000]
    try:
        with transaction.atomic(), audit.acting_as(actor, source="portal"):
            if not quotes.can_answer(revision):
                raise ValidationError(
                    f"This {noun} could no longer be answered when your answer arrived."
                )
            if data.get("answer") == "approve":
                agreement = quotes.terms_to_accept(revision)
                if agreement and data.get("terms") != agreement["hash"]:
                    raise ValidationError(
                        "The terms changed before your approval arrived. Please read them "
                        "again and approve."
                    )
                signing = AgreementSettings.load().signature_enabled
                drawn = signatures.clean(data.get("signature", "")) if signing else b""
                how = data.get("how") or "signed in"
                quotes.approve(
                    revision,
                    actor=actor,
                    approved_by_name=name,
                    approved_at=_at(event),
                    notes="\n\n".join(
                        filter(None, [f"In the portal on Cloudflare, {how}.", comment])
                    ),
                    accept_agreement=agreement is not None,
                    method="portal",
                    agreement_method="signature" if signing else "click",
                    ip=event.get("ip") or None,
                    signature=drawn,
                    picks=data.get("picks") or [],
                )
            else:
                quotes.decline(revision, actor=actor, reason=comment or "Declined in the portal.")
    except (ValidationError, TransitionError, PermissionDenied) as exc:
        message = " ".join(getattr(exc, "messages", [str(exc)]))[:500]
        EdgeAnswer.objects.create(revision=revision, message=message)


def _upload(event) -> None:
    """A file a customer sent (FR-PRT-7): fetched, taken in for review through the
    portal's own checks, and removed from Cloudflare."""
    import urllib.parse
    from types import SimpleNamespace

    from django.core.exceptions import ValidationError
    from django.core.files.uploadedfile import SimpleUploadedFile

    from trammel.jobs.models import Job
    from trammel.portal import uploads

    from .models import EdgeUpload

    data = event["data"]
    key = data.get("key", "")
    customer = _customer(event)
    if customer is not None and not EdgeUpload.objects.filter(pk=data["id"]).exists():
        job = Job.objects.filter(pk=data.get("job") or None, customer=customer).first()
        try:
            content = client.call("GET", f"/_sync/objects/{urllib.parse.quote(key)}", raw=True)
        except client.EdgeError as exc:
            if getattr(exc, "status", None) == 404:
                return  # removed by the customer before it was collected
            raise
        visitor = SimpleNamespace(customer=customer, name=data.get("sent_by") or customer.name)
        sent = SimpleUploadedFile(
            data.get("name") or "file", content, content_type=data.get("type") or None
        )
        try:
            made, problems = uploads.send(
                visitor, [sent], job=job, note=data.get("note", ""), ip=event.get("ip") or None
            )
        except ValidationError as exc:
            made, problems = [], exc.messages
        EdgeUpload.objects.create(
            id=data["id"],
            customer=customer,
            job=job,
            upload=made[0] if made else None,
            name=(data.get("name") or "file")[:255],
            reason="" if made else "; ".join(problems)[:500],
            at=_at(event),
        )
    if key:
        client.delete_object(key)


def _shopify_webhook(event) -> None:
    if not modules.is_enabled("imports"):
        return
    from trammel.imports import shopify

    data = event["data"]
    status = shopify.take_webhook(
        (data.get("body") or "").encode(), data.get("hmac", ""), data.get("shop", "")
    )
    if status != 200:
        log.warning("A Shopify webhook relayed by the edge portal was refused (%s).", status)


def _ebay_deletion(event) -> None:
    """eBay's account deletion notice: its signature checked here, with eBay's
    published key, before anything is wiped."""
    if not modules.is_enabled("imports"):
        return
    import json

    from trammel.imports import deletions, ebay
    from trammel.imports.models import DeletionRequest, EbaySettings

    data = event["data"]
    body = (data.get("body") or "").encode()
    config = EbaySettings.load()
    try:
        genuine = ebay.verify_notification(config, body, data.get("signature", ""))
    except ebay.EbayError as exc:
        raise client.EdgeError(f"Couldn't check an eBay deletion notice: {exc}") from None
    if not genuine:
        audit.record(
            "imports.deletion_refused",
            summary="Refused an eBay account deletion notice relayed by the portal on "
            "Cloudflare: its signature isn't eBay's",
            actor=access.ANONYMOUS,
        )
        return
    notice = json.loads(body)
    if (notice.get("metadata") or {}).get("topic", "MARKETPLACE_ACCOUNT_DELETION") != (
        "MARKETPLACE_ACCOUNT_DELETION"
    ):
        return
    note = notice.get("notification") or {}
    who = note.get("data") or {}
    deletions.receive(
        username=who.get("username", ""),
        user_id=who.get("userId", ""),
        eias_token=who.get("eiasToken", ""),
        notification_id=note.get("notificationId", ""),
        event_date=note.get("eventDate"),
        via=DeletionRequest.Via.EDGE,
    )


def _upload_skipped(event, exc) -> None:
    """A sent file Trammel couldn't collect: the customer is told to send it
    again, rather than seeing it on its way for good."""
    from .models import EdgeUpload

    data = event["data"]
    customer = _customer(event)
    if customer is None or not data.get("id"):
        return
    from trammel.jobs.models import Job

    EdgeUpload.objects.get_or_create(
        id=data["id"],
        defaults={
            "customer": customer,
            "job": Job.objects.filter(pk=data.get("job") or None, customer=customer).first(),
            "name": (data.get("name") or "file")[:255],
            "reason": "We couldn't collect this file. Please send it again.",
            "at": _at(event),
        },
    )


HANDLERS = {
    "quote_response": _quote_response,
    "upload": _upload,
    "shopify_webhook": _shopify_webhook,
    "ebay_deletion": _ebay_deletion,
    "code_sent": _code_sent,
    "mail_failed": _mail_failed,
    "signed_in": _signed_in,
    "access_code_used": _access_code_used,
}


#: An event that fails this many times is skipped, so one bad event can't
#: hold up everything after it.
MAX_TRIES = 5
#: What's done when an event is skipped, by kind.
SKIPPED = {"upload": _upload_skipped}
#: The events, as the activity log names them.
KINDS = {
    "quote_response": "an answer to a quote",
    "upload": "a file a customer sent",
    "shopify_webhook": "a Shopify notice",
    "ebay_deletion": "an eBay deletion notice",
}


def _apply(event) -> None:
    handler = HANDLERS.get(event["kind"])
    if handler is None:
        log.warning("Edge inbox: no handler for %s", event["kind"])
        return
    handler(event)


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
            seq = str(event["seq"])
            try:
                with transaction.atomic():
                    _apply(event)
                    EdgeSettings.objects.filter(pk=1).update(inbox_after=event["seq"])
            except Exception as exc:
                tries = settings.failures.get(seq, 0) + 1
                settings.failures = {**settings.failures, seq: tries}
                EdgeSettings.objects.filter(pk=1).update(failures=settings.failures)
                if tries < MAX_TRIES:
                    raise  # tried again next time
                what = KINDS.get(event["kind"], f"an event ({event['kind']})")
                audit.record(
                    "edge.event_skipped",
                    summary=f"Skipped {what} from the portal on Cloudflare after "
                    f"{tries} tries: {exc}"[:500],
                    actor=access.SYSTEM,
                )
                if event["kind"] in SKIPPED:
                    with transaction.atomic():
                        SKIPPED[event["kind"]](event, exc)
                EdgeSettings.objects.filter(pk=1).update(inbox_after=event["seq"])
            if seq in settings.failures:
                settings.failures = {k: v for k, v in settings.failures.items() if k != seq}
                EdgeSettings.objects.filter(pk=1).update(failures=settings.failures)
            settings.inbox_after = event["seq"]
            applied += 1
        client.ack(settings.inbox_after)
    return applied


def check() -> None:
    """Before syncing: a Worker database that isn't the one Trammel knew (made
    afresh, or deleted and made again) gets everything published again and its
    inbox read from the start; one that has lost records gets them back."""
    settings = EdgeSettings.load()
    health = client.health()
    instance = health.get("instance", "")
    known = Published.objects.count()
    if instance and instance != settings.instance:
        if settings.instance:
            log.warning("The edge portal's database is a new one: publishing everything again.")
        Published.objects.all().delete()
        EdgeSettings.objects.filter(pk=1).update(instance=instance, inbox_after=0, failures={})
    elif known and health.get("records", known) < known // 2:
        Published.objects.all().delete()


def run() -> dict:
    """One sync, if the edge portal is deployed: push, then pull. Problems are
    kept on the settings page rather than raised."""
    settings = EdgeSettings.load()
    if not settings.active:
        return {}
    try:
        check()
        counts = push()
        counts["applied"] = pull()
        if counts["applied"]:
            # What customers did shows there at once: approved, taken in, refused.
            again = push()
            counts["pushed"] += again["pushed"]
            counts["removed"] += again["removed"]
    except Exception as exc:  # noqa: BLE001 — shown on the settings page, retried next time
        EdgeSettings.objects.filter(pk=1).update(
            last_error=str(exc)[:2000], last_error_at=timezone.now()
        )
        log.warning("Edge portal sync failed: %s", exc)
        return {"error": str(exc)}
    EdgeSettings.objects.filter(pk=1).update(last_sync_at=timezone.now(), last_error="")
    return counts
