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
from trammel.core.web import document_response, document_view, permission_required
from trammel.customers.models import Customer
from trammel.jobs.models import Job
from trammel.quotes.models import Approval, QuoteRevision

from . import services

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


def _context(request, **extra) -> dict:
    return {"visitor": getattr(request, "visitor", None), **extra}


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


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

            tasks.enqueue("portal.request_code", {"email": email, "ip": client_ip(request)})
            request.session[services.PENDING_KEY] = email
            return redirect("portal:code")
    return render(request, "portal/sign_in.html", _context(request, ready=ready))


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
    return render(
        request,
        "portal/home.html",
        _context(
            request,
            jobs=jobs,
            waiting=waiting,
            open_invoices=[i for i in invoices if i.status != "paid"],
            files=_shared_files(actor, request.visitor.customer),
        ),
    )


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
        .select_related("author", "document")
        .order_by("-created_at")
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
    found = _invoice(request, pk)
    return render(request, "portal/invoice.html", _context(request, invoice=found))


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
        inline=False,
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
