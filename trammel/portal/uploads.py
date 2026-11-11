"""Files customers send through the portal (FR-PRT-7).

A file a customer sends is stored like any other, but attached to nothing: it
waits, as an `Upload`, until staff accept it onto a job or the customer (with a
type, and shown back to the customer) or decline it with a reason the customer
sees. Only the kinds of file a shop works from are taken, each no larger than
the portal's own limit, and how many a customer can send is capped.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from trammel.core import audit, modules

from .models import PortalSettings, Upload

#: What customers can send: pictures, PDFs, drawings, laser files and models.
#: Never G-code (it runs machines), archives or programs.
KINDS = frozenset({"image", "pdf", "svg", "dxf", "lightburn", "stl", "3mf", "step"})
KINDS_LABEL = "photos, PDFs, and SVG, DXF, LightBurn, STL, 3MF or STEP files"
#: Files in one send.
PER_SEND = 10
#: Files a customer can have waiting for review at once.
WAITING_PER_CUSTOMER = 50
#: Files a customer can send in a day.
PER_DAY = 100


def is_on() -> bool:
    """Customers can send files: the portal and files on, and uploads allowed."""
    return (
        modules.is_enabled("portal")
        and modules.is_enabled("files")
        and PortalSettings.load().uploads_enabled
    )


def limit_mb() -> int:
    return min(PortalSettings.load().upload_limit_mb or 1, settings.MAX_UPLOAD_MB)


def room_for(customer) -> int:
    """How many more files the customer can send now."""
    waiting = Upload.objects.filter(customer=customer, status=Upload.Status.PENDING).count()
    today = Upload.objects.filter(
        customer=customer, sent_at__gte=timezone.now() - dt.timedelta(days=1)
    ).count()
    return max(0, min(PER_SEND, WAITING_PER_CUSTOMER - waiting, PER_DAY - today))


def send(visitor, files: list, *, job=None, note: str = "", ip=None) -> tuple[list, list]:
    """Store the files a customer sent, to wait for review. Returns (the uploads
    made, the problems with any that weren't taken)."""
    from trammel.files import services as files_services

    if not is_on():
        raise ValidationError("Sending files isn't available right now.")
    customer = visitor.customer
    files = [f for f in files if f]
    if not files:
        raise ValidationError("Choose at least one file to send.")
    room = room_for(customer)
    if room == 0:
        raise ValidationError(
            "You've sent as many files as we can take for now. We'll look at the ones you've "
            "sent, and you can send more after that."
        )
    problems = []
    if len(files) > room:
        problems.append(f"Only {room} of the files could be sent this time.")
        files = files[:room]
    note = note.strip()[:2000]
    made = []
    for upload in files:
        name = (upload.name or "file").rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:255]
        try:
            with transaction.atomic():
                revision = files_services.upload(
                    upload, name=name, kinds=KINDS, limit_mb=limit_mb()
                )
                made.append(
                    Upload.objects.create(
                        customer=customer,
                        job=job,
                        document=revision.document,
                        note=note,
                        sent_by=visitor.name[:150],
                        ip=ip or None,
                    )
                )
        except ValidationError as exc:
            problems += [m if m.startswith(name) else f"{name}: {m}" for m in exc.messages]
    if made:
        where = f" for job {job.number}" if job else ""
        audit.record(
            "portal.upload",
            customer,
            summary=f"{visitor.name} sent {len(made)} file{'s' if len(made) != 1 else ''}"
            f"{where} through the portal",
        )
        transaction.on_commit(lambda: _tell_staff(made))
    return made, problems


def _tell_staff(made: list[Upload]) -> None:
    from django.urls import reverse

    from trammel.core import templating
    from trammel.notifications.services import notify

    first = made[0]
    notify(
        "portal_files",
        entity=first.customer,
        context={
            "customer": templating.customer_context(first.customer),
            "sender": first.sent_by,
            "job": {"number": first.job.number, "title": first.job.title} if first.job else None,
            "files": [u.document.title for u in made],
            "note": first.note,
            "link": settings.BASE_URL + reverse("portal:review_list"),
        },
    )


def waiting(queryset=None):
    qs = queryset if queryset is not None else Upload.objects.all()
    return qs.filter(status=Upload.Status.PENDING).select_related(
        "customer", "job", "document__current__blob"
    )


def _require_pending(upload: Upload) -> None:
    if upload.status != Upload.Status.PENDING:
        raise ValidationError("This file has already been reviewed.")


@transaction.atomic
def accept(upload: Upload, *, target, doc_type=None, title: str = "", shared=True, user=None):
    """Attach the file to `target` (the customer or one of their jobs), with its
    type and title, shown back to the customer unless `shared` is False."""
    from trammel.files import services as files_services

    upload = Upload.objects.select_for_update().get(pk=upload.pk)
    _require_pending(upload)
    customer_id = getattr(target, "customer_id", None) or getattr(target, "pk", None)
    if customer_id != upload.customer_id:
        raise ValidationError("Attach it to this customer or one of their jobs.")
    document = upload.document
    files_services.update_document(
        document,
        title=(title or document.title).strip()[:200],
        description=document.description or upload.note,
        doc_type=doc_type if doc_type is not None else document.doc_type,
    )
    link = files_services.attach(document, target, user=user)
    if link.shared_with_customer != shared:
        link.shared_with_customer = shared
        link.save(update_fields=["shared_with_customer"])
    upload.status = Upload.Status.ACCEPTED
    upload.reviewed_at, upload.reviewed_by, upload.attachment = timezone.now(), user, link
    upload.save()
    audit.record(
        "portal.upload_accept",
        upload.customer,
        summary=f"Accepted {document} from {upload.customer} onto {target}",
    )
    return upload


@transaction.atomic
def decline(upload: Upload, *, reason: str = "", user=None) -> Upload:
    """Turn the file down: it goes, and the customer sees the reason."""
    upload = Upload.objects.select_for_update().get(pk=upload.pk)
    _require_pending(upload)
    upload.status = Upload.Status.DECLINED
    upload.reason = reason.strip()[:300]
    upload.reviewed_at, upload.reviewed_by = timezone.now(), user
    upload.save()
    upload.document.delete()
    audit.record(
        "portal.upload_decline",
        upload.customer,
        summary=f"Declined {upload.document} from {upload.customer}"
        + (f": {upload.reason}" if upload.reason else ""),
    )
    return upload
