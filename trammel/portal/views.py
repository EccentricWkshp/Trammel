"""The customer portal's pages (FR-PRT-1 to FR-PRT-6).

Every page but signing in needs a signed-in customer, and every record is
fetched through the customer's own scope, so another customer's record is
"not found", never "forbidden" (FR-PRT-2). Drafts, voided invoices, internal
notes and files staff haven't shared never appear.
"""

from __future__ import annotations

from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import (
    content_disposition_header,
    url_has_allowed_host_and_scheme,
    urlencode,
)
from django.views.decorators.http import require_POST

from trammel.core import access, audit, modules, templating
from trammel.core.lifecycle import TransitionError
from trammel.core.middleware import client_ip
from trammel.core.web import (
    admin_required,
    document_response,
    document_view,
    permission_required,
)
from trammel.customers.models import Customer
from trammel.jobs.models import Job
from trammel.quotes.models import Approval, QuoteRevision

from . import services
from .models import Upload

#: What the customer sees of a job's status.
JOB_STATUS = {
    "proposal": "Being quoted",
    "quoted": "Quote sent",
    "approved": "Approved, scheduled",
    "in_progress": "In progress",
    "on_hold": "On hold",
    "completed": "Completed",
    "closed": "Closed",
    "declined": "Declined",
    "cancelled": "Canceled",
}
#: Quote revisions a customer can see: sent to them, or answered.
SHOWN_QUOTES = ("sent", "approved", "declined", "expired")
#: Invoices a customer can see: issued, not drafts or voided.
SHOWN_INVOICES = ("issued", "partially_paid", "paid")


def portal_view(view):
    """A page for a signed-in customer, acting as them (FR-PRT-2)."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        visitor = services.current(request)
        # Served from the edge portal instead: these pages are off, but staff
        # can still view them as a customer would (FR-PRT-9).
        if services.served_elsewhere() and not (visitor and visitor.is_preview):
            raise Http404
        if visitor is None:
            return redirect("portal:sign_in")
        request.visitor = visitor
        # Staff viewing the portal as the customer would can look, not act:
        # nothing is approved, declined or changed in the customer's name.
        if visitor.is_preview and request.method not in ("GET", "HEAD"):
            messages.error(
                request,
                f"You're viewing the portal as {visitor.customer} sees it, so nothing can be "
                "approved, declined or changed here.",
            )
            back = request.META.get("HTTP_REFERER", "")
            if not url_has_allowed_host_and_scheme(
                back, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                back = reverse("portal:home")
            return redirect(back)
        request.actor = visitor.actor
        audit.rebind(visitor.actor, source="portal")
        response = view(request, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    return wrapped


def signing_in(view):
    """A sign-in page: off while customers are served elsewhere (the edge portal)."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if services.served_elsewhere():
            raise Http404
        return view(request, *args, **kwargs)

    return wrapped


def _context(request, **extra) -> dict:
    return {"visitor": getattr(request, "visitor", None), **extra}


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


@signing_in
def sign_in(request):
    """Ask for an address to send a code to (FR-PRT-1)."""
    if services.current(request):
        return redirect("portal:home")
    ready = services.email_ready()
    if request.method == "POST" and ready:
        email = services.normalize(request.POST.get("email", ""))
        if "@" not in email:
            messages.error(request, "Enter your email address.")
        else:
            # Looked up and sent by the worker: the answer takes the same
            # time whether or not the address can sign in.
            from trammel.core import tasks

            tasks.enqueue_public("portal.request_code", email, client_ip(request))
            request.session[services.PENDING_KEY] = email
            return redirect("portal:code")
    return render(request, "portal/sign_in.html", _context(request, ready=ready))


@signing_in
def code(request):
    """Enter the code that was emailed."""
    email = request.session.get(services.PENDING_KEY)
    if not email:
        return redirect("portal:sign_in")
    error = ""
    if request.method == "POST":
        customers = services.verify_code(email, request.POST.get("code", ""), client_ip(request))
        if len(customers) == 1:
            services.sign_in(request, customers[0], email)
            return redirect("portal:home")
        if customers:
            request.session[services.CHOICE_KEY] = {
                "email": email,
                "customers": [str(c.pk) for c in customers],
                "since": timezone.now().isoformat(),
            }
            return redirect("portal:choose")
        error = (
            "That code didn't work. Codes expire after "
            f"{services.SignInCode.LIFETIME_MINUTES} minutes and work once; ask for a new one "
            "if you need to."
        )
    return render(request, "portal/code.html", _context(request, email=email, error=error))


@signing_in
def choose(request):
    """An address that signs in for more than one customer picks which."""
    import datetime as dt

    choice = request.session.get(services.CHOICE_KEY) or {}
    try:
        fresh = timezone.now() - dt.datetime.fromisoformat(choice["since"]) < dt.timedelta(
            minutes=10
        )
    except KeyError, TypeError, ValueError:
        fresh = False
    allowed = {
        str(c.pk): c
        for c in services.customers_for(choice.get("email", ""))
        if str(c.pk) in choice.get("customers", [])
    }
    if not fresh or not allowed:
        services.sign_out(request)
        return redirect("portal:sign_in")
    if request.method == "POST" and (picked := allowed.get(request.POST.get("customer", ""))):
        services.sign_in(request, picked, choice["email"])
        return redirect("portal:home")
    return render(
        request, "portal/choose.html", _context(request, customers=list(allowed.values()))
    )


@signing_in
def access_code(request):
    """Sign in with a code staff gave the customer (FR-PRT-10)."""
    if services.current(request):
        return redirect("portal:home")
    error = ""
    if request.method == "POST":
        if services.use_code(request, request.POST.get("code", ""), client_ip(request)):
            return redirect("portal:home")
        error = (
            "That code didn't work. A code from us works once and expires after "
            f"{services.AccessCode.LIFETIME_DAYS} days; ask us for a new one if you need to."
        )
    return render(
        request,
        "portal/access_code.html",
        _context(request, error=error, email_ready=services.email_ready()),
    )


@signing_in
@require_POST
def sign_out(request):
    services.sign_out(request)
    messages.success(request, "You're signed out.")
    return redirect("portal:sign_in")


# ---------------------------------------------------------------------------
# What the customer sees
# ---------------------------------------------------------------------------


def _quotes(actor, **filters):
    return (
        access.scoped(actor, "quotes.view", QuoteRevision.objects.all())
        .filter(status__in=SHOWN_QUOTES, **filters)
        .select_related("quote__job")
        .order_by("-sent_at", "-created_at")
    )


def _invoices(actor, **filters):
    if not modules.is_enabled("billing"):
        return []
    from trammel.billing.models import Invoice

    return (
        access.scoped(actor, "billing.view", Invoice.objects.all())
        .filter(status__in=SHOWN_INVOICES, **filters)
        .order_by("-invoice_date", "-created_at")
    )


def _shared_files(actor, target) -> list:
    if not modules.is_enabled("files"):
        return []
    from trammel.files.models import Attachment

    return list(
        Attachment.objects.filter(
            content_type=ContentType.objects.get_for_model(target),
            object_id=target.pk,
            shared_with_customer=True,
            document__deleted_at__isnull=True,
            document__current__isnull=False,
        ).select_related("document__current")
    )


def _agreements(actor, **filters):
    if not modules.is_enabled("agreements"):
        return []
    from trammel.agreements.models import CustomerAgreement

    return access.scoped(actor, "agreements.view", CustomerAgreement.objects.all()).filter(
        status="accepted", **filters
    )


def _with_status(jobs):
    for job in jobs:
        job.portal_status = JOB_STATUS.get(job.status, job.get_status_display())
    return jobs


@portal_view
def home(request):
    actor = request.actor
    jobs = _with_status(
        list(access.scoped(actor, "jobs.view", Job.objects.all()).order_by("-created_at")[:50])
    )
    today = timezone.localdate()
    waiting = [
        r for r in _quotes(actor, status="sent") if not (r.valid_until and r.valid_until < today)
    ]
    invoices = _invoices(actor)
    if invoices and modules.is_enabled("billing"):
        from trammel.billing import services as billing

        for found in invoices:
            found.pay_options = billing.pay_options(found)
    return render(
        request,
        "portal/home.html",
        _context(
            request,
            jobs=jobs,
            waiting=waiting,
            open_invoices=[i for i in invoices if i.status != "paid"],
            files=_shared_files(actor, request.visitor.customer),
            **_sending(request, None),
        ),
    )


def _sending(request, job) -> dict:
    """What the customer has sent (on a job, or all of it from the account
    page), and whether they can send more (FR-PRT-7)."""
    from . import uploads

    customer = request.visitor.customer
    sent = Upload.objects.filter(customer=customer).select_related("job", "document__current__blob")
    if job is not None:
        sent = sent.filter(job=job)
    return {
        "sent": list(sent[:30]),
        "can_send": uploads.is_on(),
        "send_limit_mb": uploads.limit_mb(),
        "send_kinds": uploads.KINDS_LABEL,
        "send_per": uploads.PER_SEND,
    }


@portal_view
def job(request, pk):
    actor = request.actor
    found = access.get_or_404(actor, "jobs.view", Job.objects.all(), pk=pk)
    _with_status([found])
    return render(
        request,
        "portal/job.html",
        _context(
            request,
            job=found,
            quotes=_quotes(actor, quote__job=found),
            invoices=_invoices(actor, job=found),
            agreements=_agreements(actor, job=found),
            files=_shared_files(actor, found),
            updates=_updates(actor, found),
            **_sending(request, found),
        ),
    )


def _updates(actor, job) -> list:
    """Notes staff shared with the customer from the job's work orders (FR-PRT-4)."""
    if not modules.is_enabled("workorders"):
        return []
    from trammel.workorders.models import WorkOrderNote

    return list(
        access.scoped(actor, "workorders.view", WorkOrderNote.objects.all())
        .filter(work_order__job=job, shared_with_customer=True)
        .select_related("author", "document__current__blob")
        .order_by("-created_at")
    )


@portal_view
@require_POST
def send_files(request):
    """Files the customer sends, to wait for review (FR-PRT-7)."""
    from . import uploads

    job = None
    if request.POST.get("job"):
        job = access.get_or_404(
            request.actor, "jobs.view", Job.objects.all(), pk=request.POST["job"]
        )
    back = reverse("portal:job", args=[job.pk]) if job else reverse("portal:home")
    try:
        made, problems = uploads.send(
            request.visitor,
            request.FILES.getlist("files"),
            job=job,
            note=request.POST.get("note", ""),
            ip=client_ip(request),
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        if made:
            count = f"{len(made)} file{'s' if len(made) != 1 else ''}"
            messages.success(request, f"Thank you. We've received {count} and will look soon.")
        for problem in problems:
            messages.error(request, problem)
    return redirect(f"{back}#send-files")


def _sent(request, pk) -> Upload:
    sent = get_object_or_404(
        Upload.objects.select_related("document__current__blob"),
        pk=pk,
        customer=request.visitor.customer,
    )
    if sent.status == Upload.Status.DECLINED or sent.document.current is None:
        raise Http404
    return sent


@portal_view
def sent_file(request, pk):
    """A file the customer sent, back to them."""
    from trammel.files import services as files
    from trammel.files.views import _serve

    revision = _sent(request, pk).document.current
    return _serve(
        files.blob_path(revision.blob),
        mime=revision.blob.mime,
        filename=revision.original_name,
        # Pictures and PDFs can be viewed in the browser.
        inline=request.GET.get("inline") == "1" and revision.blob.is_previewable_inline,
    )


@portal_view
def sent_thumbnail(request, pk):
    return _thumbnail(_sent(request, pk).document.current.blob)


@portal_view
def sent_thumbnail_status(request, pk):
    from trammel.files import services as files

    return files.status_response(_sent(request, pk).document.current.blob)


def _shared_document(request, pk):
    from trammel.files.models import Document

    if not modules.is_enabled("files"):
        raise Http404
    document = get_object_or_404(Document.objects.select_related("current__blob"), pk=pk)
    if document.current is None or not _shared_with(request.actor, document):
        raise Http404
    return document


@portal_view
def file_thumbnail(request, pk):
    """The preview of a file shared with the customer."""
    return _thumbnail(_shared_document(request, pk).current.blob)


@portal_view
def file_thumbnail_status(request, pk):
    from trammel.files import services as files

    return files.status_response(_shared_document(request, pk).current.blob)


def _thumbnail(blob):
    from trammel.files import services as files
    from trammel.files.views import _serve

    if blob.kind == "svg":
        return _serve(files.blob_path(blob), mime=blob.mime, filename="preview.svg", inline=True)
    if not blob.thumbnail:
        raise Http404
    return _serve(
        files.media_root() / blob.thumbnail,
        mime=files.thumbnail_mime(blob),
        filename="preview",
        inline=True,
    )


@portal_view
def note_photo(request, pk):
    """The photo on a note shared with the customer."""
    if not modules.is_enabled("workorders"):
        raise Http404
    from trammel.files import services as files
    from trammel.files.views import _serve
    from trammel.workorders.models import WorkOrderNote

    note = access.get_or_404(
        request.actor,
        "workorders.view",
        WorkOrderNote.objects.select_related("document__current__blob"),
        pk=pk,
        shared_with_customer=True,
        document__isnull=False,
    )
    current = note.document.current
    if current is None:
        raise Http404
    return _serve(
        files.blob_path(current.blob),
        mime=current.blob.mime,
        filename=current.original_name,
        inline=current.blob.is_previewable_inline,
    )


def _revision(request, pk) -> QuoteRevision:
    revision = access.get_or_404(
        request.actor,
        "quotes.view",
        QuoteRevision.objects.select_related("quote__job__customer"),
        pk=pk,
    )
    if revision.status not in SHOWN_QUOTES:
        raise Http404
    return revision


def _can_respond(revision) -> bool:
    from trammel.quotes import services as quotes

    return quotes.can_answer(revision)


def _agreement_to_accept(revision):
    """The terms the customer accepts with this quote, if any: the exact text
    approving records (quotes.terms_to_accept)."""
    from trammel.quotes import services as quotes

    return quotes.terms_to_accept(revision)


def _signing() -> bool:
    """Customers draw a signature to approve, when signature capture is on."""
    from trammel.agreements.models import AgreementSettings

    return AgreementSettings.load().signature_enabled


@portal_view
def quote(request, pk):
    from trammel.quotes import services as quotes

    revision = _revision(request, pk)
    can_respond = _can_respond(revision)
    return render(
        request,
        "portal/quote.html",
        _context(
            request,
            revision=revision,
            can_respond=can_respond,
            agreement=_agreement_to_accept(revision) if can_respond else None,
            # The options and alternatives to pick before approving (FR-QUO-11).
            choosing=quotes.choosing(revision, quotes.picks_from(request.GET))
            if can_respond
            else None,
            signing=_signing(),
            approval=Approval.objects.filter(revision=revision).first(),
        ),
    )


@portal_view
@require_POST
def respond(request, pk):
    """Approve or decline a quote or change order (FR-PRT-3)."""
    from trammel.quotes import services as quotes

    revision = _revision(request, pk)
    noun = revision.quote.noun
    comment = request.POST.get("comment", "").strip()[:2000]
    if not _can_respond(revision):
        messages.error(request, f"This {noun} can no longer be answered here.")
        return redirect("portal:quote", pk=revision.pk)
    try:
        if request.POST.get("answer") == "approve":
            name = request.POST.get("name", "").strip()[:150]
            agreement = _agreement_to_accept(revision)
            if agreement and request.POST.get("terms") != agreement["hash"]:
                raise ValidationError(
                    "The terms have changed since this page was opened. Please read them again."
                )
            if agreement and request.POST.get("accept") != "1":
                raise ValidationError(f"Tick the box to accept the {agreement['title'].lower()}.")
            from trammel.core import signature as signatures

            signing = _signing()
            drawn = signatures.clean(request.POST.get("signature", "")) if signing else b""
            quotes.approve(
                revision,
                actor=request.actor,
                approved_by_name=name,
                notes="\n\n".join(
                    filter(None, [f"In the portal, {request.visitor.how}.", comment])
                ),
                accept_agreement=agreement is not None,
                method="portal",
                agreement_method="signature" if signing else "click",
                ip=client_ip(request),
                signature=drawn,
                picks=quotes.picks_from(request.POST),
            )
            messages.success(request, f"Thank you. The {noun} is approved.")
        elif request.POST.get("answer") == "decline":
            reason = comment or "Declined in the portal."
            quotes.decline(revision, actor=request.actor, reason=reason)
            messages.success(request, f"The {noun} is declined. Thank you for letting us know.")
        else:
            raise ValidationError("Choose to approve or decline.")
    except (ValidationError, TransitionError) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        # Back to the page with the customer's choices still picked.
        picks = quotes.picks_from(request.POST)
        if picks:
            query = urlencode([("pick", p) for p in picks])
            return redirect(f"{reverse('portal:quote', args=[revision.pk])}?{query}#choices")
    except PermissionDenied:
        raise Http404 from None
    return redirect("portal:quote", pk=revision.pk)


@portal_view
@document_view
def quote_document(request, pk):
    from trammel.quotes import services as quotes

    revision = _revision(request, pk)
    try:
        # As the customer has picked their choices on the quote page (FR-QUO-11).
        html = quotes.render_html(revision, quotes.picks_from(request.GET))
    except templating.TemplateProblem:
        html = "<p>This document can't be shown right now. Please contact us for a copy.</p>"
    return document_response(request, html)


def _pdf(data: bytes, filename: str) -> HttpResponse:
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = content_disposition_header(True, f"{filename}")
    return response


@portal_view
def quote_pdf(request, pk):
    from trammel.quotes import services as quotes

    revision = _revision(request, pk)
    try:
        data = quotes.render_pdf(revision, quotes.picks_from(request.GET))
    except templating.PdfUnavailable, templating.TemplateProblem:
        messages.error(request, "The PDF can't be made right now. Please contact us for a copy.")
        return redirect("portal:quote", pk=revision.pk)
    return _pdf(data, f"{revision.quote.number}-r{revision.number}.pdf")


def _invoice(request, pk):
    from trammel.billing.models import Invoice

    if not modules.is_enabled("billing"):
        raise Http404
    invoice = access.get_or_404(request.actor, "billing.view", Invoice.objects.all(), pk=pk)
    if invoice.status not in SHOWN_INVOICES:
        raise Http404
    return invoice


@portal_view
def invoice(request, pk):
    from trammel.billing import services as billing

    found = _invoice(request, pk)
    # Back from paying online: only this fixed text is ever shown from a link.
    if request.GET.get("notice") == "payment-received":
        messages.success(
            request,
            "Thank you. Your payment is being confirmed and will show here shortly.",
        )
    return render(
        request,
        "portal/invoice.html",
        _context(request, invoice=found, pay_options=billing.pay_options(found)),
    )


@portal_view
@document_view
def invoice_document(request, pk):
    from trammel.billing import services as billing

    found = _invoice(request, pk)
    try:
        html = billing.render_html(found)
    except templating.TemplateProblem:
        html = "<p>This document can't be shown right now. Please contact us for a copy.</p>"
    return document_response(request, html)


@portal_view
def invoice_pdf(request, pk):
    from trammel.billing import services as billing

    found = _invoice(request, pk)
    try:
        data = templating.render_pdf(billing.render_html(found))
    except templating.PdfUnavailable, templating.TemplateProblem:
        messages.error(request, "The PDF can't be made right now. Please contact us for a copy.")
        return redirect("portal:invoice", pk=found.pk)
    return _pdf(data, f"{found.number}.pdf")


@portal_view
def agreement_pdf(request, pk):
    from django.template.loader import render_to_string

    from trammel.agreements.models import CustomerAgreement
    from trammel.core.models import BusinessProfile

    if not modules.is_enabled("agreements"):
        raise Http404
    found = access.get_or_404(
        request.actor,
        "agreements.view",
        CustomerAgreement.objects.select_related("job__customer"),
        pk=pk,
        status="accepted",
    )
    html = render_to_string(
        "agreements/print.html", {"agreement": found, "business": BusinessProfile.load()}
    )
    try:
        data = templating.render_pdf(html)
    except templating.PdfUnavailable:
        messages.error(request, "The PDF can't be made right now. Please contact us for a copy.")
        return redirect("portal:job", pk=found.job_id)
    return _pdf(data, f"agreement-{found.job.number}.pdf")


@portal_view
def file(request, pk):
    """A file staff shared with the customer (FR-PRT-4, FR-DOC-8)."""
    if not modules.is_enabled("files"):
        raise Http404
    from trammel.files import services as files
    from trammel.files.models import Document
    from trammel.files.views import _serve

    document = get_object_or_404(
        Document.objects.select_related("current__blob"), pk=pk, current__isnull=False
    )
    if not _shared_with(request.actor, document):
        raise Http404
    revision = document.current
    return _serve(
        files.blob_path(revision.blob),
        mime=revision.blob.mime,
        filename=revision.original_name,
        inline=request.GET.get("inline") == "1" and revision.blob.is_previewable_inline,
    )


#: The records whose shared files the portal shows: the customer and their jobs.
SHARED_FROM = ("customers.customer", "jobs.job")


def _shared_with(actor, document) -> bool:
    """Shared with the customer through their own record or one of their jobs —
    never through a draft quote or invoice they can't see."""
    from trammel.files import services as files

    for attached in document.attachments.filter(shared_with_customer=True).select_related(
        "content_type"
    ):
        if f"{attached.content_type.app_label}.{attached.content_type.model}" not in SHARED_FROM:
            continue
        target = attached.target
        perm = files.target_permission(target) if target is not None else None
        if perm and access.has_perm(actor, perm, target):
            return True
    return False


# ---------------------------------------------------------------------------
# Staff: who may use the portal
# ---------------------------------------------------------------------------


@permission_required("portal.manage")
@require_POST
def set_access(request, customer_pk):
    customer = access.get_or_404(
        request.actor, "customers.view", Customer.objects.all(), pk=customer_pk
    )
    enabled = request.POST.get("enabled") == "1"
    if enabled and (customer.is_internal or customer.is_placeholder):
        messages.error(request, "This customer can't use the portal.")
    else:
        services.set_access(customer, enabled, user=request.user)
        if enabled and not services.signers_of(customer):
            messages.warning(
                request,
                "Portal access is on, but nobody can get an emailed sign-in code yet: add an "
                "email address to the customer or one of their contacts, or make a code to "
                "give them.",
            )
        else:
            messages.success(request, f"Portal access {'turned on' if enabled else 'turned off'}.")
    return redirect(f"{reverse('customers:detail', args=[customer.pk])}")


@permission_required("portal.manage")
@require_POST
def make_code(request, customer_pk):
    """Make a sign-in code to give the customer, shown this once (FR-PRT-10)."""
    customer = access.get_or_404(
        request.actor, "customers.view", Customer.objects.all(), pk=customer_pk
    )
    if customer.is_internal or customer.is_placeholder:
        raise PermissionDenied
    try:
        code, row = services.make_code(customer, user=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("customers:detail", pk=customer.pk)
    response = render(
        request,
        "portal/code_made.html",
        {
            "customer": customer,
            "code": code,
            "row": row,
            "sign_in_url": services.link(reverse("portal:access_code")),
        },
    )
    response["Cache-Control"] = "private, no-store"
    return response


@permission_required("portal.manage")
@require_POST
def cancel_code(request, customer_pk):
    customer = access.get_or_404(
        request.actor, "customers.view", Customer.objects.all(), pk=customer_pk
    )
    if services.cancel_codes(customer, why=f"Canceled the portal sign-in code for {customer}"):
        messages.success(request, "The sign-in code is canceled.")
    return redirect("customers:detail", pk=customer.pk)


def _reviewable(request):
    """Uploads of customers this staff member can see."""
    return Upload.objects.filter(
        customer__in=access.scoped(request.actor, "customers.view", Customer.objects.all())
    ).select_related("customer", "job", "reviewed_by", "document__current__blob")


@permission_required("files.upload")
def review_list(request):
    """Files customers have sent: those waiting for review, then the latest reviewed."""
    from . import uploads

    found = _reviewable(request)
    return render(
        request,
        "portal/review_list.html",
        {
            "waiting": list(uploads.waiting(found).order_by("sent_at")),
            "reviewed": list(
                found.exclude(status=Upload.Status.PENDING).order_by("-reviewed_at")[:20]
            ),
        },
    )


@permission_required("files.upload")
def review(request, pk):
    """Accept a customer's file onto a job or the customer, or decline it."""
    from trammel.files.models import DocumentType

    from . import uploads

    found = get_object_or_404(_reviewable(request), pk=pk)
    customer = found.customer
    jobs = list(customer.jobs.order_by("-created_at")[:50])
    if request.method == "POST" and found.status == Upload.Status.PENDING:
        try:
            if request.POST.get("action") == "decline":
                uploads.decline(found, reason=request.POST.get("reason", ""), user=request.user)
                messages.success(request, f"Declined {found.document.title}.")
            else:
                where = request.POST.get("target", "")
                target = next((j for j in jobs if str(j.pk) == where), None) or customer
                doc_type = DocumentType.objects.filter(
                    pk=request.POST.get("doc_type") or None, is_active=True
                ).first()
                uploads.accept(
                    found,
                    target=target,
                    doc_type=doc_type,
                    title=request.POST.get("title", ""),
                    shared=request.POST.get("shared") == "1",
                    user=request.user,
                )
                messages.success(request, f"Accepted {found.document.title} onto {target}.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect("portal:review", pk=found.pk)
        following = uploads.waiting(_reviewable(request)).order_by("sent_at").first()
        return (
            redirect("portal:review", pk=following.pk)
            if following
            else redirect("portal:review_list")
        )
    return render(
        request,
        "portal/review.html",
        {
            "upload": found,
            "revision": found.document.current,
            "jobs": jobs,
            "types": DocumentType.objects.filter(is_active=True),
            "others": uploads.waiting(_reviewable(request)).exclude(pk=found.pk).count(),
        },
    )


@admin_required
@require_POST
def upload_settings(request):
    """Whether customers can send files, and how large (System page)."""
    from .forms import UploadSettingsForm
    from .models import PortalSettings

    form = UploadSettingsForm(request.POST, instance=PortalSettings.load())
    if form.is_valid():
        from trammel.core.services import save_audited

        before = audit.snapshot(PortalSettings.load())
        save_audited(form.instance, before=before)
        messages.success(request, "Portal file settings saved.")
    else:
        messages.error(request, " ".join(e for errors in form.errors.values() for e in errors))
    return redirect("core:system")


@login_required
@require_POST
def preview(request, customer_pk):
    """Staff view the portal as the customer would, read-only."""
    customer = access.get_or_404(
        request.actor, "customers.view", Customer.objects.all(), pk=customer_pk
    )
    if not services.can_preview(request.actor, customer):
        raise PermissionDenied
    services.start_preview(request, customer)
    return redirect("portal:home")


@require_POST
def preview_stop(request):
    """Back to the customer's page from viewing the portal as them."""
    customer_pk = services.stop_preview(request)
    if customer_pk and request.user.is_authenticated:
        return redirect("customers:detail", pk=customer_pk)
    return redirect("portal:sign_in")
