# No `from __future__ import annotations` here: list_uploads is wrapped by
# Ninja's paginate decorator (see trammel/jobs/api.py).
"""The customer portal's staff side over the API (FR-API-1): who may use the
portal, sign-in codes to give out (FR-PRT-10), reviewing the files customers
send (FR-PRT-7), and what they may send."""

import datetime as dt
from typing import Literal
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse
from ninja import Router, Schema, Status
from ninja.pagination import LimitOffsetPagination, paginate

from trammel.core import access, audit
from trammel.customers.models import Customer

from . import services, uploads
from .models import Access, PortalSettings, Upload

router = Router(tags=["customer portal"])

UploadStatus = Literal["pending", "accepted", "declined"]


# ---------------------------------------------------------------------------
# Who may use the portal
# ---------------------------------------------------------------------------


class SignerOut(Schema):
    name: str
    email: str


class WaitingCodeOut(Schema):
    created_at: dt.datetime
    created_by: str
    expires_at: dt.datetime


class AccessOut(Schema):
    customer: UUID
    enabled: bool
    changed_at: dt.datetime | None
    changed_by: str
    last_sign_in_at: dt.datetime | None
    #: Who can get an emailed sign-in code: the customer's address and its contacts'.
    signers: list[SignerOut]
    #: A code from staff still waiting to be used (the code itself is never kept).
    waiting_code: WaitingCodeOut | None
    #: Codes can be emailed: email is set up.
    email_ready: bool
    portal_url: str
    #: Turned on, but nobody can get an emailed code yet.
    warning: str = ""


class AccessIn(Schema):
    enabled: bool


class CodeOut(Schema):
    """Shown this once: give it to the customer. Only its hash is kept."""

    code: str
    expires_at: dt.datetime
    sign_in_url: str


class CanceledOut(Schema):
    canceled: int


def _customer(request, customer_id) -> Customer:
    return access.get_or_404(
        request.actor, "customers.view", Customer.objects.all(), pk=customer_id
    )


def _access(customer: Customer) -> dict:
    row = Access.objects.filter(customer=customer).select_related("changed_by").first()
    enabled = bool(row and row.enabled)
    code = services.waiting_code(customer) if enabled else None
    signers = services.signers_of(customer)
    return {
        "customer": customer.pk,
        "enabled": enabled,
        "changed_at": row.changed_at if row else None,
        "changed_by": row.changed_by.display_name if row and row.changed_by else "",
        "last_sign_in_at": row.last_sign_in_at if row else None,
        "signers": [{"name": n, "email": e} for n, e in signers],
        "waiting_code": (
            {
                "created_at": code.created_at,
                "created_by": code.created_by.display_name if code.created_by else "",
                "expires_at": code.expires_at,
            }
            if code
            else None
        ),
        "email_ready": services.email_ready(),
        "portal_url": services.link(),
        "warning": (
            "Portal access is on, but nobody can get an emailed sign-in code yet: add an email "
            "address to the customer or one of their contacts, or make a code to give them."
            if enabled and not signers
            else ""
        ),
    }


def _not_for_portal(customer: Customer) -> None:
    if customer.is_internal or customer.is_placeholder:
        raise ValidationError("This customer can't use the portal.")


@router.get("/customers/{customer_id}/access", response=AccessOut)
def get_access(request, customer_id: UUID):
    customer = _customer(request, customer_id)
    _not_for_portal(customer)
    return _access(customer)


@router.put("/customers/{customer_id}/access", response=AccessOut)
def set_access(request, customer_id: UUID, payload: AccessIn):
    """Turn the customer's portal access on or off. Off cancels any code waiting."""
    access.require(request.actor, "portal.manage")
    customer = _customer(request, customer_id)
    _not_for_portal(customer)
    services.set_access(customer, payload.enabled, user=request.actor.user)
    return _access(customer)


@router.post("/customers/{customer_id}/codes", response={201: CodeOut})
def make_code(request, customer_id: UUID):
    """A one-time sign-in code to give the customer (by phone, on paper),
    replacing any still waiting. Good for 7 days."""
    access.require(request.actor, "portal.manage")
    customer = _customer(request, customer_id)
    _not_for_portal(customer)
    try:
        code, row = services.make_code(customer, user=request.actor.user)
    except ValueError as exc:
        raise ValidationError(str(exc)) from None
    return Status(
        201,
        {
            "code": code,
            "expires_at": row.expires_at,
            "sign_in_url": services.link(reverse("portal:access_code")),
        },
    )


@router.delete("/customers/{customer_id}/codes", response=CanceledOut)
def cancel_codes(request, customer_id: UUID):
    access.require(request.actor, "portal.manage")
    customer = _customer(request, customer_id)
    canceled = services.cancel_codes(
        customer, why=f"Canceled the portal sign-in code for {customer}"
    )
    return {"canceled": canceled}


# ---------------------------------------------------------------------------
# Files customers sent
# ---------------------------------------------------------------------------


class UploadOut(Schema):
    id: UUID
    customer: UUID
    customer_name: str
    job: UUID | None
    job_number: str
    status: UploadStatus
    title: str
    original_name: str
    type: str
    size: int
    #: The file's revision: GET /files/revisions/{id}/download (also ./file here).
    revision_id: UUID | None
    note: str
    sent_by: str
    sent_at: dt.datetime
    ip: str | None
    reviewed_at: dt.datetime | None
    reviewed_by: str
    #: Why it was declined (shown to the customer).
    reason: str
    #: Accepted: the document and what it's attached to.
    document: UUID | None
    attached_to: str
    shared_with_customer: bool | None


class AcceptIn(Schema):
    #: The customer, or one of their jobs.
    target: Literal["customer"] | UUID = "customer"
    doc_type: UUID | None = None
    title: str = ""
    #: Shown back to the customer in the portal.
    shared: bool = True


class DeclineIn(Schema):
    #: Shown to the customer.
    reason: str = ""


def _reviewable(request):
    return Upload.objects.filter(
        customer__in=access.scoped(request.actor, "customers.view", Customer.objects.all())
    ).select_related(
        "customer", "job", "reviewed_by", "document__current__blob", "attachment__document"
    )


def _upload_out(upload: Upload) -> dict:
    declined = upload.status == Upload.Status.DECLINED
    revision = None if declined else upload.document.current
    link = upload.attachment
    return {
        "id": upload.pk,
        "customer": upload.customer_id,
        "customer_name": upload.customer.name,
        "job": upload.job_id,
        "job_number": upload.job.number if upload.job else "",
        "status": upload.status,
        "title": "" if declined else upload.document.title,
        "original_name": revision.original_name if revision else "",
        "type": revision.blob.mime if revision else "",
        "size": revision.blob.size if revision else 0,
        "revision_id": revision.pk if revision else None,
        "note": upload.note,
        "sent_by": upload.sent_by,
        "sent_at": upload.sent_at,
        "ip": upload.ip,
        "reviewed_at": upload.reviewed_at,
        "reviewed_by": upload.reviewed_by.display_name if upload.reviewed_by else "",
        "reason": upload.reason,
        "document": link.document_id if link else None,
        "attached_to": str(link.target) if link and link.target is not None else "",
        "shared_with_customer": link.shared_with_customer if link else None,
    }


def _upload(request, upload_id) -> Upload:
    access.require(request.actor, "files.upload")
    return get_object_or_404(_reviewable(request), pk=upload_id)


@router.get("/uploads", response=list[UploadOut])
@paginate(LimitOffsetPagination)
def list_uploads(
    request,
    status: UploadStatus | None = None,
    customer: UUID | None = None,
    job: UUID | None = None,
):
    """Files customers sent: waiting ones oldest first, then reviewed ones
    newest first. `status=pending` is what's waiting for review."""
    access.require(request.actor, "files.upload")
    qs = _reviewable(request)
    if status:
        qs = qs.filter(status=status)
    if customer:
        qs = qs.filter(customer_id=customer)
    if job:
        qs = qs.filter(job_id=job)
    waiting = [u for u in qs.filter(status=Upload.Status.PENDING).order_by("sent_at")]
    reviewed = list(qs.exclude(status=Upload.Status.PENDING).order_by("-reviewed_at"))
    return [_upload_out(u) for u in waiting + reviewed]


@router.get("/uploads/{upload_id}", response=UploadOut)
def get_upload(request, upload_id: UUID):
    return _upload_out(_upload(request, upload_id))


@router.get("/uploads/{upload_id}/file")
def upload_file(request, upload_id: UUID):
    """The file itself, as sent. Gone once declined."""
    from trammel.files.api import serve_revision

    found = _upload(request, upload_id)
    if found.status == Upload.Status.DECLINED or found.document.current is None:
        raise Http404
    return serve_revision(found.document.current)


@router.post("/uploads/{upload_id}/accept", response=UploadOut)
def accept_upload(request, upload_id: UUID, payload: AcceptIn):
    """Attach it to the customer or one of their jobs, with a type and title."""
    from trammel.files.models import DocumentType

    found = _upload(request, upload_id)
    if payload.target == "customer":
        target = found.customer
    else:
        target = found.customer.jobs.filter(pk=payload.target).first()
        if target is None:
            raise ValidationError({"target": ["Not one of this customer's jobs."]})
    doc_type = None
    if payload.doc_type:
        doc_type = DocumentType.objects.filter(pk=payload.doc_type, is_active=True).first()
        if doc_type is None:
            raise ValidationError({"doc_type": ["No such document type."]})
    uploads.accept(
        found,
        target=target,
        doc_type=doc_type,
        title=payload.title,
        shared=payload.shared,
        user=request.actor.user,
    )
    return _upload_out(_upload(request, upload_id))


@router.post("/uploads/{upload_id}/decline", response=UploadOut)
def decline_upload(request, upload_id: UUID, payload: DeclineIn):
    """Turn it down: the file goes, and the customer sees the reason."""
    found = _upload(request, upload_id)
    uploads.decline(found, reason=payload.reason, user=request.actor.user)
    return _upload_out(_upload(request, upload_id))


# ---------------------------------------------------------------------------
# What customers may send
# ---------------------------------------------------------------------------


class PortalSettingsOut(Schema):
    #: The portal module is on.
    portal_on: bool
    #: Served from elsewhere (the portal on Cloudflare): its address.
    served_from: str
    uploads_enabled: bool
    upload_limit_mb: int
    #: The kinds of file customers can send (checked by content, not name).
    kinds: list[str]
    kinds_label: str


class PortalSettingsIn(Schema):
    uploads_enabled: bool | None = None
    upload_limit_mb: int | None = None


def _settings() -> dict:
    row = PortalSettings.load()
    return {
        "portal_on": services.is_on(),
        "served_from": services.served_elsewhere(),
        "uploads_enabled": row.uploads_enabled,
        "upload_limit_mb": row.upload_limit_mb,
        "kinds": sorted(uploads.KINDS),
        "kinds_label": uploads.KINDS_LABEL,
    }


@router.get("/settings", response=PortalSettingsOut)
def get_settings(request):
    if not any(access.has_perm(request.actor, p) for p in ("portal.manage", "files.upload")):
        raise PermissionDenied
    return _settings()


@router.patch("/settings", response=PortalSettingsOut)
def update_settings(request, payload: PortalSettingsIn):
    """Whether customers can send files, and how large (the System page)."""
    from trammel.core.services import save_audited

    from .forms import UploadSettingsForm

    access.require(request.actor, "portal.manage")
    row = PortalSettings.load()
    data = {"uploads_enabled": row.uploads_enabled, "upload_limit_mb": row.upload_limit_mb}
    data.update(payload.dict(exclude_unset=True, exclude_none=True))
    if not data["uploads_enabled"]:
        data.pop("uploads_enabled")  # an unchecked box, as the form expects
    form = UploadSettingsForm(data, instance=row)
    if not form.is_valid():
        raise ValidationError({f: list(e) for f, e in form.errors.items()})
    save_audited(form.instance, before=audit.snapshot(PortalSettings.load()))
    return _settings()
