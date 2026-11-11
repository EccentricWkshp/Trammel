from __future__ import annotations

from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_POST

from trammel.catalog import services as catalog
from trammel.core import (
    access,
    archive,
    audit,
    modules,
    panels,
    placeholders,
    sorting,
    templating,
)
from trammel.core.lifecycle import TransitionError
from trammel.core.services import save_audited
from trammel.core.web import document_response, document_view, paginate, permission_required
from trammel.jobs import services as jobs
from trammel.jobs.models import TERMINAL, Job

from . import services
from .estimators import ESTIMATORS
from .forms import (
    LINE_FORMS,
    ApprovalForm,
    ManualPriceFields,
    OfferForm,
    QuoteForJobForm,
    QuoteSettingsForm,
    QuoteTemplateForm,
    QuoteWithNewJobForm,
    RevisionForm,
)
from .models import Quote, QuoteLine, QuoteRevision, QuoteSettings, QuoteTemplate, Status


def _revision(request, perm, pk) -> QuoteRevision:
    return access.get_or_404(
        request.actor,
        perm,
        QuoteRevision.objects.select_related("quote__job__customer", "template"),
        pk=pk,
    )


def _draft(request, pk) -> QuoteRevision:
    revision = _revision(request, "quotes.edit", pk)
    if not revision.is_draft:
        messages.error(request, "This revision has been sent and cannot be changed.")
        raise _Redirect(revision)
    return revision


class _Redirect(Exception):
    def __init__(self, target):
        self.target = target


def _handles_redirect(view):
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except _Redirect as r:
            return redirect(r.target)

    wrapped.__name__ = view.__name__
    return wrapped


SORTS = sorting.Sorts(
    {
        "number": sorting.By("Number", "quote__number"),
        "customer": sorting.By("Customer", "quote__job__customer__name"),
        "status": sorting.By("Status", sorting.in_order("status", Status.values)),
        "valid": sorting.By("Valid until", "valid_until"),
        "total": sorting.By("Total", "total", newest_first=True),
        "created": sorting.By("Date created", "created_at", newest_first=True),
    },
    default="created",
)


@permission_required("quotes.view")
def quote_list(request):
    kind = request.GET.get("kind", Quote.Kind.QUOTE)
    if kind not in Quote.Kind.values:
        kind = Quote.Kind.QUOTE
    qs = QuoteRevision.objects.select_related("quote__job__customer").filter(quote__kind=kind)
    qs = qs.exclude(status=Status.SUPERSEDED)
    status = request.GET.get("status", "")
    if status in Status.values:
        qs = qs.filter(status=status)
    if term := request.GET.get("q", "").strip():
        qs = (
            qs.filter(quote__number__icontains=term)
            | qs.filter(quote__title__icontains=term)
            | qs.filter(quote__job__customer__name__icontains=term)
        )
    qs, sort = SORTS.apply(request, access.scoped(request.actor, "quotes.view", qs))
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "quotes/list.html",
        {
            "kind": kind,
            "tabs": [
                {"key": k, "label": f"{label}s", "url": f"?kind={k}", "active": k == kind}
                for k, label in Quote.Kind.choices
            ],
            "page": paginate(request, qs),
            "status": status,
            "statuses": Status.choices,
            "q": request.GET.get("q", ""),
            "query": params.urlencode(),
            **sort,
        },
    )


@permission_required("quotes.create")
def quote_new(request):
    """Start a quote from the quotes page: for an open job, or with a new job."""
    actor = request.actor
    open_jobs = access.scoped(
        actor, "jobs.view", Job.objects.exclude(status__in=TERMINAL).select_related("customer")
    ).order_by("-created_at")
    can_create_job = access.has_perm(actor, "jobs.create")
    mode = request.POST.get("mode") if request.method == "POST" else None
    existing = QuoteForJobForm(
        request.POST if mode == "existing" else None,
        jobs=open_jobs,
        initial={"job": request.GET.get("job")},
    )
    fresh = (
        QuoteWithNewJobForm(request.POST if mode == "new" else None, prefix="new")
        if can_create_job
        else None
    )
    revision = None
    try:
        if mode == "existing" and existing.is_valid():
            d = existing.cleaned_data
            revision = services.create_quote(d["job"], title=d["title"])
        elif mode == "new" and fresh is not None and fresh.is_valid():
            d = fresh.cleaned_data
            with transaction.atomic():
                job = jobs.create_job(
                    Job(title=d["title"], customer=d["customer"], due_date=d["due_date"])
                )
                revision = services.create_quote(job)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    if revision is not None:
        quote = revision.quote
        messages.success(request, f"{quote.get_kind_display()} {quote.number} created.")
        return redirect(revision)
    return render(
        request,
        "quotes/new.html",
        {"existing": existing, "fresh": fresh, "has_jobs": open_jobs.exists()},
    )


@permission_required("quotes.create")
@require_POST
def quote_create(request, job_pk):
    job = access.get_or_404(request.actor, "jobs.view", Job.objects.all(), pk=job_pk)
    try:
        revision = services.create_quote(job, title=request.POST.get("title", ""))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(job)
    return redirect(revision)


@permission_required("quotes.view")
def revision_detail(request, pk):
    revision = _revision(request, "quotes.view", pk)
    actor = request.actor
    quote = revision.quote
    can_edit = revision.is_draft and access.has_perm(actor, "quotes.edit", revision)
    can_costs = access.has_perm(actor, "core.see_costs")
    lines = list(revision.lines.select_related("product", "machine", "material", "tax_category"))
    margin = None
    if can_costs and revision.subtotal:
        margin = ((revision.subtotal - revision.cost_total) / revision.subtotal * 100).quantize(
            revision.subtotal.__class__("0.1")
        )
    return render(
        request,
        "quotes/revision.html",
        {
            "revision": revision,
            "quote": revision.quote,
            "job": revision.quote.job,
            "lines": lines,
            "has_choices": any(line.offer != QuoteLine.Offer.INCLUDED for line in lines),
            "form": RevisionForm(instance=revision) if can_edit else None,
            "can_edit": can_edit,
            "can_costs": can_costs,
            "margin": margin,
            "line_kinds": [(k, QuoteLine.Kind(k).label) for k in LINE_FORMS],
            "estimators": ESTIMATORS.values(),
            "can_send": revision.is_draft and access.has_perm(actor, "quotes.edit", revision),
            "can_approve": revision.status == Status.SENT
            and access.has_perm(actor, services.approve_permission(quote), revision),
            "can_sign": services.can_answer(revision)
            and _signing()
            and access.has_perm(actor, services.approve_permission(quote), revision),
            "can_delete": access.has_perm(actor, "quotes.edit", revision)
            and not services.can_delete(quote),
            "can_revise": revision.status in (Status.SENT, Status.DECLINED, Status.EXPIRED)
            and access.has_perm(actor, "quotes.edit", revision),
            "history": revision.quote.revisions.order_by("-number"),
            "approval": getattr(revision, "approval", None)
            if revision.status == Status.APPROVED
            else None,
            **panels.tabbed(
                request,
                [],
                first=[
                    ("lines", "Lines"),
                    ("details", "Details"),
                    ("preview", "Preview"),
                    *([("revisions", "Revisions")] if revision.quote.revisions.count() > 1 else []),
                    ("files", "Files"),
                ],
                obj=revision.quote,
            ),
        },
    )


@permission_required("quotes.edit")
@_handles_redirect
@require_POST
def revision_update(request, pk):
    revision = _draft(request, pk)
    before = audit.snapshot(revision)
    form = RevisionForm(request.POST, instance=revision)
    if form.is_valid():
        try:
            services.update_revision(form.save(commit=False), before=before)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, "Quote details saved.")
    else:
        for field, errors in form.errors.items():
            messages.error(request, f"{field}: {' '.join(errors)}")
    return redirect(revision)


@permission_required("quotes.edit")
@_handles_redirect
@require_POST
def description(request, pk):
    """Save the draft's narrative description, from the box above its lines."""
    revision = _draft(request, pk)
    before = audit.snapshot(revision)
    revision.description = request.POST.get("description", "").strip()
    try:
        services.update_revision(revision, before=before)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Description saved.")
    return redirect(revision)


@permission_required("quotes.edit")
@_handles_redirect
@require_POST
def shipping(request, pk):
    revision = _draft(request, pk)
    before = audit.snapshot(revision)
    try:
        revision.shipping = Decimal(request.POST.get("shipping") or "0")
        services.update_revision(revision, before=before)
    except ArithmeticError, ValueError:
        messages.error(request, "Enter the shipping charge as an amount, such as 12.50.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(revision)


@permission_required("quotes.edit")
@_handles_redirect
def line_edit(request, pk, kind=None, line_pk=None):
    revision = _draft(request, pk)
    line = revision.lines.filter(pk=line_pk).first() if line_pk else None
    if line_pk and line is None:
        raise Http404
    kind = line.kind if line else kind
    if line and line.kind == QuoteLine.Kind.ESTIMATE:
        return redirect("quotes:estimate", pk=revision.pk, key=line.estimator, line_pk=line.pk)
    form_class = LINE_FORMS.get(kind)
    if form_class is None:
        raise Http404
    form = form_class(request.POST or None, initial=form_class.initial_from(line) if line else None)
    if revision.quote.is_change_order and hasattr(form, "allow_credit"):
        form.allow_credit()
    can_catalog = modules.is_enabled("catalog") and access.has_perm(request.actor, "catalog.edit")
    if not can_catalog:
        form.fields.pop("save_to_catalog", None)
    if not can_catalog or line is not None:
        getattr(form, "drop_new_product", lambda: None)()
    if request.method == "POST" and form.is_valid():
        note = ""
        try:
            with transaction.atomic():
                if getattr(form, "wants_new_product", False):
                    try:
                        product = catalog.create_product(**form.new_product())
                    except ValidationError as exc:
                        # The product's own field names are not this form's.
                        raise ValidationError(exc.messages) from exc
                    form.cleaned_data["product"] = product
                    note = f"Added “{product}” to the catalog."
                built = form.build(revision, line)
                services.save_line(built)
                if form.cleaned_data.get("save_to_catalog"):
                    product, created = catalog.save_adhoc(
                        description=built.description,
                        price=built.unit_price,
                        cost=built.unit_cost,
                        unit=built.unit,
                        tax_category=built.tax_category,
                    )
                    note = (
                        f"Added “{product}” to the catalog."
                        if created
                        else f"“{product}” is already in the catalog."
                    )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            if note:
                messages.success(request, note)
            return redirect(revision)
    title = f"{'Edit' if line else 'Add'} {QuoteLine.Kind(kind).label.lower()} — {revision}"
    return render(
        request, "quotes/line_form.html", {"form": form, "title": title, "revision": revision}
    )


@permission_required("quotes.edit")
@_handles_redirect
@require_POST
def line_delete(request, pk, line_pk):
    revision = _draft(request, pk)
    line = revision.lines.filter(pk=line_pk).first()
    if line is None:
        raise Http404
    services.delete_line(line)
    return redirect(revision)


@permission_required("quotes.edit")
@_handles_redirect
def line_offer(request, pk, line_pk):
    """Offer a line as included, optional, or an alternative (FR-QUO-11)."""
    revision = _draft(request, pk)
    line = revision.lines.filter(pk=line_pk).first()
    if line is None:
        raise Http404
    form = OfferForm(
        request.POST or None,
        initial={
            "offer": line.offer,
            "group": line.group,
            "default": line.is_alternate and line.included,
        },
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            services.set_offer(line, d["offer"], d["group"], d["default"])
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            return redirect(revision)
    return render(
        request,
        "quotes/offer.html",
        {
            "form": form,
            "line": line,
            "revision": revision,
            "groups": services.groups(revision),
        },
    )


@permission_required("quotes.edit")
@_handles_redirect
def estimate(request, pk, key, line_pk=None):
    revision = _draft(request, pk)
    estimator = ESTIMATORS.get(key)
    if estimator is None:
        raise Http404
    line = revision.lines.filter(pk=line_pk).first() if line_pk else None
    form_class = estimator.form_class()
    initial = line.inputs if line else None
    form = form_class(request.POST or None, initial=initial)
    price_form = ManualPriceFields(
        request.POST or None, initial=ManualPriceFields.manual_initial(line)
    )
    preview = None
    if request.method == "POST" and form.is_valid() and price_form.is_valid():
        built = price_form.apply_manual_price(
            services.estimate_line(revision, key, form.cleaned_data)
        )
        if "add" in request.POST:
            if line is not None:
                built.pk, built.position = line.pk, line.position
                built.offer, built.group, built.included = line.offer, line.group, line.included
                built._state.adding = False
            services.save_estimate(built)
            messages.success(request, f"{built.description} added.")
            return redirect(revision)
        preview = built
    return render(
        request,
        "quotes/estimate.html",
        {
            "form": form,
            "price_form": price_form,
            "estimator": estimator,
            "revision": revision,
            "preview": preview,
            "can_costs": access.has_perm(request.actor, "core.see_costs"),
        },
    )


@permission_required("quotes.edit")
@require_POST
def send(request, pk):
    revision = _revision(request, "quotes.edit", pk)
    try:
        services.send(revision, actor=request.actor)
    except TransitionError as exc:
        for p in exc.problems:
            messages.error(request, p)
    else:
        messages.success(request, f"{revision} marked as sent. It can no longer be changed.")
    return redirect(revision)


@permission_required("quotes.view")
def approve(request, pk):
    revision = _revision(request, "quotes.view", pk)
    quote = revision.quote
    access.require(request.actor, services.approve_permission(quote), revision)
    from trammel.agreements import services as agreements

    needs_agreement = not quote.is_change_order and not agreements.satisfied(quote.job)
    form = ApprovalForm(
        request.POST or None,
        initial={
            "approved_by_name": revision.quote.job.customer.name,
            "approved_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
        },
    )
    if quote.is_change_order:
        del form.fields["accept_agreement"]
    picks = services.picks_from(request.POST if request.method == "POST" else request.GET)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            services.approve(
                revision,
                actor=request.actor,
                approved_by_name=d["approved_by_name"],
                how=d["how"],
                approved_at=d["approved_at"],
                notes=d["notes"],
                accept_agreement=d.get("accept_agreement", False),
                user=request.user,
                picks=picks,
            )
        except (ValidationError, TransitionError) as exc:
            form.add_error(None, getattr(exc, "problems", None) or exc.messages)
        except PermissionDenied:
            form.add_error(None, f"Your role does not allow approving {quote.noun}s.")
        else:
            messages.success(request, f"{revision} approved.")
            return redirect(revision)
    return render(
        request,
        "quotes/approve.html",
        {
            "form": form,
            "revision": revision,
            "quote": quote,
            "needs_agreement": needs_agreement,
            "choosing": services.choosing(revision, picks)
            if revision.status == Status.SENT
            else None,
        },
    )


def _signing() -> bool:
    from trammel.agreements.models import AgreementSettings

    return AgreementSettings.load().signature_enabled


@permission_required("quotes.view")
def sign(request, pk):
    """The customer signs on this device (a shop tablet or phone) to approve,
    accepting the terms with their signature (FR-AGR-7). A plain page with no
    staff menus, made to hand over the counter."""
    from trammel.core import signature as signatures
    from trammel.core.middleware import client_ip

    revision = _revision(request, "quotes.view", pk)
    quote = revision.quote
    access.require(request.actor, services.approve_permission(quote), revision)
    if not _signing():
        messages.error(request, "Signature capture is turned off in agreement settings.")
        return redirect(revision)
    if not services.can_answer(revision):
        messages.error(request, f"This {quote.noun} can't be approved any more.")
        return redirect(revision)
    terms = services.terms_to_accept(revision)
    error = ""
    name = request.POST.get("name", "").strip()[:150]
    picks = services.picks_from(request.POST if request.method == "POST" else request.GET)
    if request.method == "POST":
        try:
            if terms and request.POST.get("terms") != terms["hash"]:
                raise ValidationError(
                    "The terms have changed since this page was opened. Please read them again."
                )
            if terms and request.POST.get("accept") != "1":
                raise ValidationError(f"Tick the box to accept the {terms['title'].lower()}.")
            if not name:
                raise ValidationError("Write your full name.")
            drawn = signatures.clean(request.POST.get("signature", ""))
            services.approve(
                revision,
                actor=request.actor,
                approved_by_name=name,
                how="in_person",
                notes=f"Signed on a device in the shop, handed over by {request.user}.",
                accept_agreement=terms is not None,
                method="signed",
                agreement_method="signature",
                ip=client_ip(request),
                user=request.user,
                signature=drawn,
                picks=picks,
            )
        except (ValidationError, TransitionError) as exc:
            error = " ".join(getattr(exc, "problems", None) or exc.messages)
        except PermissionDenied:
            error = f"Your role does not allow approving {quote.noun}s."
        else:
            return render(request, "quotes/sign_done.html", {"revision": revision, "name": name})
    return render(
        request,
        "quotes/sign.html",
        {
            "revision": revision,
            "quote": quote,
            "terms": terms,
            "name": name or quote.job.customer.name,
            "error": error,
            "choosing": services.choosing(revision, picks),
        },
    )


@permission_required("quotes.edit")
def quote_delete(request, pk):
    """Delete a quote or change order nothing has happened to: never sent."""
    revision = _revision(request, "quotes.edit", pk)
    quote = revision.quote
    refuse = services.can_delete(quote)
    if request.method == "POST" and not refuse:
        try:
            services.delete_quote(quote, reason=request.POST.get("reason", ""))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect(revision)
        messages.success(request, f"{quote} moved to the trash. It can be restored for 30 days.")
        return redirect(quote.job)
    return render(
        request,
        "core/confirm_delete.html",
        {
            "obj": quote,
            "noun": quote.noun,
            "used": bool(refuse),
            "refuse": refuse,
            "archive_url": "",
            "back": revision.get_absolute_url(),
            "parts": "its revisions",
        },
    )


@permission_required("quotes.edit")
@require_POST
def decline(request, pk):
    revision = _revision(request, "quotes.edit", pk)
    try:
        services.decline(revision, actor=request.actor, reason=request.POST.get("reason", ""))
    except TransitionError as exc:
        messages.error(request, " ".join(exc.problems))
    return redirect(revision)


@permission_required("quotes.edit")
@require_POST
def revise(request, pk):
    revision = _revision(request, "quotes.edit", pk)
    try:
        new = services.revise(revision, actor=request.actor)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(revision)
    messages.success(request, f"Revision {new.number} started.")
    return redirect(new)


@permission_required("quotes.view")
@document_view
def document(request, pk):
    revision = _revision(request, "quotes.view", pk)
    try:
        html = services.render_html(revision, services.picks_from(request.GET))
    except templating.TemplateProblem as exc:
        html = f"<p>The quote template has a problem: {escape(str(exc))}</p>"
    return document_response(request, html)


@permission_required("quotes.view")
def pdf(request, pk):
    revision = _revision(request, "quotes.view", pk)
    try:
        data = services.render_pdf(revision, services.picks_from(request.GET))
    except (templating.PdfUnavailable, templating.TemplateProblem) as exc:
        messages.error(request, str(exc))
        return redirect(revision)
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = content_disposition_header(
        True, f"{revision.quote.number}-r{revision.number}.pdf"
    )
    return response


@permission_required("quotes.view")
def compare(request, pk, other_pk):
    a = _revision(request, "quotes.view", pk)
    b = _revision(request, "quotes.view", other_pk)
    if a.quote_id != b.quote_id:
        raise Http404
    a, b = sorted((a, b), key=lambda r: r.number)
    rows = []
    a_lines = {line.description: line for line in a.lines.all()}
    b_lines = {line.description: line for line in b.lines.all()}
    for desc in dict.fromkeys([*a_lines, *b_lines]):
        la, lb = a_lines.get(desc), b_lines.get(desc)
        changed = not (la and lb and la.quantity == lb.quantity and la.line_total == lb.line_total)
        rows.append({"description": desc, "a": la, "b": lb, "changed": changed})
    return render(request, "quotes/compare.html", {"a": a, "b": b, "rows": rows})


@permission_required("quotes.manage_templates")
def template_list(request):
    return render(
        request,
        "quotes/templates.html",
        {
            "templates": QuoteTemplate.objects.all(),
            "settings": QuoteSettings.load(),
        },
    )


@permission_required("quotes.manage_templates")
def template_edit(request, pk=None):
    obj = QuoteTemplate.objects.filter(pk=pk).first() if pk else QuoteTemplate()
    if pk and obj is None:
        raise Http404
    before = audit.snapshot(obj) if pk else None
    form = QuoteTemplateForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        problems = templating.validate(form.cleaned_data["body"])
        if problems:
            form.add_error("body", problems)
        else:
            template = save_audited(form.save(commit=False), before=before)
            if template.is_default:
                QuoteTemplate.objects.exclude(pk=template.pk).update(is_default=False)
            messages.success(request, f"{template} saved.")
            return redirect("quotes:templates")
    return render(
        request,
        "core/template_editor.html",
        {
            "form": form,
            "title": f"Edit {obj}" if pk else "New quote template",
            "back": "quotes:templates",
            "back_label": "Quote templates",
            **placeholders.editor_context("quote"),
            **(
                {
                    "obj": obj,
                    "archive_url": reverse("quotes:template_archive", args=[pk]),
                    "delete_url": reverse("quotes:template_delete", args=[pk]),
                }
                if pk
                else {}
            ),
        },
    )


@permission_required("quotes.manage_templates")
def settings_edit(request):
    obj = QuoteSettings.load()
    before = audit.snapshot(obj)
    form = QuoteSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        save_audited(form.save(commit=False), before=before, summary="Edited quote settings")
        return redirect("quotes:templates")
    return render(
        request,
        "core/settings_form.html",
        {"form": form, "title": "Quote settings", "back": "quotes:templates"},
    )


def _template_refusal(template) -> str:
    if template.is_default:
        return "This is the default template. Make another template the default first."
    return ""


@permission_required("quotes.manage_templates")
@require_POST
def template_archive(request, pk):
    template = get_object_or_404(QuoteTemplate, pk=pk)
    return archive.archive_view(
        request,
        template,
        back=reverse("quotes:template_edit", args=[pk]),
        refuse=_template_refusal(template),
    )


@permission_required("quotes.manage_templates")
def template_delete(request, pk):
    template = get_object_or_404(QuoteTemplate, pk=pk)
    return archive.delete_view(
        request,
        template,
        archive_url=reverse("quotes:template_archive", args=[pk]),
        done_url=reverse("quotes:templates"),
        back=reverse("quotes:template_edit", args=[pk]),
        refuse=_template_refusal(template),
    )
