from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from trammel.catalog import services as catalog
from trammel.catalog.models import Product
from trammel.core import access, archive, audit, modules, panels, placeholders, templating, units
from trammel.core.lifecycle import TransitionError
from trammel.core.money import MoneyInput
from trammel.core.services import save_audited
from trammel.core.web import (
    csv_response,
    document_response,
    document_view,
    paginate,
    permission_required,
)
from trammel.customers.models import Customer
from trammel.jobs.models import Job
from trammel.quotes.models import QuoteRevision
from trammel.tax import services as tax
from trammel.tax.models import TaxCategory

from . import services
from .models import OPEN, Invoice, InvoiceTemplate, Payment, Status

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class InvoiceForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = [
            "invoice_date",
            "terms_days",
            "fulfillment",
            "ship_to",
            "shipping",
            "manual_jurisdiction",
            "template",
            "notes",
            "internal_notes",
        ]
        widgets = {
            "invoice_date": DATE,
            "notes": forms.Textarea(attrs={"rows": 2}),
            "internal_notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["ship_to"].queryset = self.instance.customer.addresses.all()
        self.fields["template"].queryset = InvoiceTemplate.objects.filter(
            Q(is_active=True) | Q(pk=self.instance.template_id)
        )


class LineForm(forms.Form):
    product = forms.ModelChoiceField(
        Product.objects.filter(is_active=True),
        required=False,
        help_text="Or describe an item below.",
    )
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    quantity = forms.DecimalField(max_digits=12, decimal_places=4, initial=1)
    unit = forms.ChoiceField(choices=units.UNIT_CHOICES, initial="each")
    unit_price = MoneyInput(max_digits=14, decimal_places=4, required=False)
    discount_pct = forms.DecimalField(
        max_digits=7, decimal_places=3, min_value=0, max_value=100, required=False, initial=0
    )
    tax_category = forms.ModelChoiceField(TaxCategory.objects.all(), required=False)
    save_to_catalog = forms.BooleanField(
        required=False, help_text="Also add this item to the product catalog."
    )

    def __init__(self, *args, can_catalog: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if not can_catalog:
            del self.fields["save_to_catalog"]

    def clean(self):
        data = super().clean()
        product = data.get("product")
        if not product and (not data.get("description") or data.get("unit_price") is None):
            raise forms.ValidationError("Pick a product, or give a description and a price.")
        if product and data.get("save_to_catalog"):
            data["save_to_catalog"] = False  # already in the catalog
        return data

    def line_fields(self) -> dict:
        d = self.cleaned_data
        p = d["product"]
        return {
            "description": d["description"] or (p.name if p else ""),
            "quantity": d["quantity"],
            "unit": p.unit if p and not d["description"] else d["unit"],
            "unit_price": d["unit_price"] if d["unit_price"] is not None else p.price,
            "discount_pct": d["discount_pct"] or Decimal("0"),
            "tax_category": d["tax_category"] or (p.tax_category if p else None),
            "product": p,
            "category": "product" if p else "other",
            "source_type": "product" if p else "adhoc",
        }


class PaymentForm(forms.Form):
    received_on = forms.DateField(widget=DATE, initial=timezone.localdate)
    amount = MoneyInput(max_digits=14, decimal_places=2)
    method = forms.ChoiceField(choices=Payment.Method.choices)
    reference = forms.CharField(required=False)
    notes = forms.CharField(required=False)


class TemplateForm(forms.ModelForm):
    class Meta:
        model = InvoiceTemplate
        fields = ["name", "is_default", "body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 24, "class": "code"})}


def _invoice(request, perm, pk) -> Invoice:
    return access.get_or_404(
        request.actor, perm, Invoice.objects.select_related("customer", "job"), pk=pk
    )


def _filtered(request):
    qs = Invoice.objects.select_related("customer", "job")
    f = request.GET
    status = f.get("status", "open")
    if status == "open":
        qs = qs.filter(status__in=OPEN)
    elif status == "overdue":
        qs = qs.filter(status__in=OPEN, due_date__lt=timezone.localdate())
    elif status in Status.values:
        qs = qs.filter(status=status)
    if f.get("kind") in Invoice.Kind.values:
        qs = qs.filter(kind=f["kind"])
    if term := f.get("q", "").strip():
        qs = qs.filter(number__icontains=term) | qs.filter(customer__name__icontains=term)
    return access.scoped(request.actor, "billing.view", qs).order_by("-created_at"), status


@permission_required("billing.view")
def invoice_list(request):
    qs, status = _filtered(request)
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "billing/list.html",
        {
            "page": paginate(request, qs),
            "status": status,
            "statuses": Status.choices,
            "kinds": Invoice.Kind.choices,
            "filters": request.GET,
            "query": params.urlencode(),
        },
    )


@permission_required("billing.view")
def invoice_csv(request):
    qs, _ = _filtered(request)
    audit.record("export", entity_type="billing.invoice", summary="Exported invoices as CSV")
    rows = (
        (
            i.number,
            i.get_kind_display(),
            i.customer.name,
            i.job.number if i.job else "",
            i.get_status_display(),
            i.invoice_date,
            i.due_date,
            i.subtotal,
            i.tax_total,
            i.total,
            i.balance,
            i.tax_location_code,
        )
        for i in qs.iterator()
    )
    return csv_response(
        "invoices.csv",
        [
            "number",
            "kind",
            "customer",
            "job",
            "status",
            "date",
            "due",
            "subtotal",
            "tax",
            "total",
            "balance",
            "tax_location",
        ],
        rows,
    )


@permission_required("billing.create")
@require_POST
def invoice_create(request):
    kind = request.POST.get("kind", Invoice.Kind.FINAL)
    if kind not in (
        Invoice.Kind.FINAL,
        Invoice.Kind.PROGRESS,
        Invoice.Kind.DIRECT_SALE,
        Invoice.Kind.DEPOSIT,
    ):
        raise Http404
    job = None
    if job_pk := request.POST.get("job"):
        job = access.get_or_404(request.actor, "jobs.view", Job.objects.all(), pk=job_pk)
        customer = job.customer
    else:
        customer = access.get_or_404(
            request.actor, "customers.view", Customer.objects.all(), pk=request.POST.get("customer")
        )
        kind = Invoice.Kind.DIRECT_SALE
    try:
        invoice = services.create(customer, kind=kind, job=job)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(job or customer)
    if job and kind == Invoice.Kind.FINAL:
        for rev in QuoteRevision.objects.filter(quote__job=job, status="approved"):
            services.add_from_quote(invoice, rev)
        services.add_deposit_credits(invoice)
    return redirect(invoice)


@permission_required("billing.view")
def invoice_detail(request, pk):
    invoice = _invoice(request, "billing.view", pk)
    actor = request.actor
    draft = invoice.is_draft and access.has_perm(actor, "billing.create", invoice)
    preview = None
    if invoice.is_draft:
        preview = services.determine_tax(invoice, invoice.invoice_date or timezone.localdate())
        tax_label = services.tax_label_for(preview)
        tax_lines = tax.calculate(invoice.taxable_total, preview) if preview.resolved else []
    else:
        tax_label = invoice.tax_label
        tax_lines = list(invoice.taxes.all())
    approved = []
    if draft and invoice.job:
        approved = QuoteRevision.objects.filter(
            quote__job=invoice.job, status="approved"
        ).select_related("quote")
    return render(
        request,
        "billing/detail.html",
        {
            "invoice": invoice,
            "lines": invoice.lines.select_related("tax_category", "product"),
            "tax_label": tax_label,
            "tax_lines": tax_lines,
            "allocations": invoice.allocations.select_related("payment", "credit_note"),
            "write_offs": invoice.write_offs.all(),
            "credit_notes": invoice.credit_notes.all(),
            "panels": panels.for_object(request, invoice),
            "draft": draft,
            "form": InvoiceForm(instance=invoice) if draft else None,
            "line_form": LineForm(can_catalog=_can_catalog(request)) if draft else None,
            "preview": preview,
            "approved": approved,
            "can_issue": draft and access.has_perm(actor, "billing.issue", invoice),
            "can_void": invoice.status == Status.ISSUED and access.has_perm(actor, "billing.void"),
            "can_pay": invoice.status in OPEN
            and not invoice.is_credit_note
            and access.has_perm(actor, "billing.record_payments"),
            "can_write_off": invoice.status in OPEN
            and not invoice.is_credit_note
            and access.has_perm(actor, "billing.write_off"),
            "can_credit": invoice.status in (*OPEN, Status.PAID)
            and not invoice.is_credit_note
            and access.has_perm(actor, "billing.create"),
            "credit_available": services.customer_credit(invoice.customer)
            if invoice.status in OPEN
            else 0,
            "payment_form": PaymentForm(initial={"amount": invoice.balance}),
            **panels.tabbed(
                request,
                [],
                first=[
                    ("lines", "Lines"),
                    *([] if invoice.is_draft else [("payments", "Payments and credits")]),
                    ("details", "Details"),
                    ("preview", "Preview"),
                    ("files", "Files"),
                ],
            ),
        },
    )


@permission_required("billing.create")
@require_POST
def invoice_update(request, pk):
    invoice = _invoice(request, "billing.create", pk)
    if not invoice.is_draft:
        raise PermissionDenied
    before = audit.snapshot(invoice)
    form = InvoiceForm(request.POST, instance=invoice)
    if form.is_valid():
        save_audited(form.save(commit=False), before=before)
        services.recalculate(invoice)
    else:
        for field, errors in form.errors.items():
            messages.error(request, f"{field}: {' '.join(errors)}")
    return redirect(invoice)


@permission_required("billing.create")
@require_POST
def shipping(request, pk):
    invoice = _invoice(request, "billing.create", pk)
    try:
        amount = Decimal(request.POST.get("shipping") or "0")
        services.set_shipping(invoice, amount)
    except ArithmeticError, ValueError:
        messages.error(request, "Enter the shipping charge as an amount, such as 12.50.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(invoice)


def _can_catalog(request) -> bool:
    return modules.is_enabled("catalog") and access.has_perm(request.actor, "catalog.edit")


@permission_required("billing.create")
@require_POST
def line_add(request, pk):
    invoice = _invoice(request, "billing.create", pk)
    form = LineForm(request.POST, can_catalog=_can_catalog(request))
    if form.is_valid():
        fields = form.line_fields()
        try:
            with transaction.atomic():
                if form.cleaned_data.get("save_to_catalog"):
                    product, created = catalog.save_adhoc(
                        description=fields["description"],
                        price=fields["unit_price"],
                        unit=fields["unit"],
                        tax_category=fields["tax_category"],
                    )
                    fields |= {"product": product, "category": "product"}
                    messages.success(
                        request,
                        f"Added “{product}” to the catalog."
                        if created
                        else f"“{product}” is already in the catalog; this line uses it.",
                    )
                services.add_line(invoice, **fields)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect(invoice)


@permission_required("billing.create")
@require_POST
def line_delete(request, pk, line_pk):
    invoice = _invoice(request, "billing.create", pk)
    line = invoice.lines.filter(pk=line_pk).first()
    if line is None:
        raise Http404
    try:
        services.delete_line(line)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(invoice)


@permission_required("billing.create")
@require_POST
def pull(request, pk):
    """Add lines from the job's quotes and change orders, time or materials (FR-BIL-1)."""
    invoice = _invoice(request, "billing.create", pk)
    what = request.POST.get("what")
    try:
        if what == "quote":
            rev = QuoteRevision.objects.filter(
                pk=request.POST.get("revision") or None, quote__job=invoice.job, status="approved"
            ).first()
            if rev is None:
                raise ValidationError("Choose an approved quote.")
            try:
                percent = Decimal(request.POST.get("percent") or "100")
            except InvalidOperation:
                percent = Decimal("100")
            added = services.add_from_quote(invoice, rev, percent=percent)
        elif what == "time":
            added = services.add_unbilled_time(invoice)
        elif what == "materials":
            added = services.add_unbilled_materials(invoice)
        elif what == "deposits":
            added = services.add_deposit_credits(invoice)
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"Added {added} line(s)." if added else "Nothing new to add.")
    return redirect(invoice)


@permission_required("billing.issue")
@require_POST
def issue(request, pk):
    invoice = _invoice(request, "billing.issue", pk)
    try:
        invoice = services.issue(invoice, actor=request.actor)
    except (ValidationError, TransitionError) as exc:
        messages.error(request, " ".join(getattr(exc, "problems", None) or exc.messages))
    else:
        messages.success(request, f"{invoice.number} issued.")
    return redirect(invoice)


@permission_required("billing.void")
@require_POST
def void(request, pk):
    invoice = _invoice(request, "billing.void", pk)
    try:
        services.void(invoice, actor=request.actor, reason=request.POST.get("reason", ""))
    except (ValidationError, TransitionError) as exc:
        messages.error(request, " ".join(getattr(exc, "problems", None) or exc.messages))
    return redirect(invoice)


@permission_required("billing.create")
@require_POST
def delete_draft(request, pk):
    invoice = _invoice(request, "billing.create", pk)
    if not invoice.is_draft:
        raise PermissionDenied
    invoice.delete()
    audit.record_delete(invoice)
    messages.success(request, "Draft deleted.")
    return redirect(invoice.job or "billing:list")


@permission_required("billing.record_payments")
@require_POST
def pay(request, pk):
    invoice = _invoice(request, "billing.record_payments", pk)
    form = PaymentForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        try:
            services.record_payment(
                invoice.customer,
                amount=d["amount"],
                received_on=d["received_on"],
                method=d["method"],
                reference=d["reference"],
                notes=d["notes"],
                allocations={invoice: min(d["amount"], invoice.balance)},
                user=request.user,
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            extra = d["amount"] - invoice.balance
            messages.success(
                request,
                "Payment recorded."
                + (f" {extra} is held as customer credit." if extra > 0 else ""),
            )
    else:
        messages.error(request, "Enter the date, amount and method.")
    return redirect(f"{invoice.get_absolute_url()}?tab=payments")


@permission_required("billing.record_payments")
@require_POST
def apply_credit(request, pk):
    invoice = _invoice(request, "billing.record_payments", pk)
    try:
        services.apply_credit(invoice, invoice.balance, user=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(f"{invoice.get_absolute_url()}?tab=payments")


@permission_required("billing.write_off")
@require_POST
def write_off(request, pk):
    invoice = _invoice(request, "billing.write_off", pk)
    try:
        amount = Decimal(request.POST["amount"]) if request.POST.get("amount") else None
        services.write_off(
            invoice, amount=amount, reason=request.POST.get("reason", ""), user=request.user
        )
    except (ValidationError, InvalidOperation) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", ["Enter an amount."])))
    return redirect(f"{invoice.get_absolute_url()}?tab=payments")


@permission_required("billing.create")
@require_POST
def credit_note(request, pk):
    invoice = _invoice(request, "billing.create", pk)
    try:
        note = services.create_credit_note(
            invoice,
            amount=Decimal(request.POST.get("amount", "0")),
            description=request.POST.get("description", ""),
        )
    except (ValidationError, InvalidOperation) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", ["Enter an amount."])))
        return redirect(invoice)
    return redirect(note)


@permission_required("billing.view")
@document_view
def document(request, pk):
    invoice = _invoice(request, "billing.view", pk)
    try:
        html = services.render_html(invoice)
    except templating.TemplateProblem as exc:
        html = f"<p>The invoice template has a problem: {exc}</p>"
    return document_response(request, html)


@permission_required("billing.view")
def pdf(request, pk):
    invoice = _invoice(request, "billing.view", pk)
    try:
        data = templating.render_pdf(services.render_html(invoice))
    except (templating.PdfUnavailable, templating.TemplateProblem) as exc:
        messages.error(request, str(exc))
        return redirect(invoice)
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{invoice.number or "draft"}.pdf"'
    return response


@permission_required("billing.record_payments")
def customer_payment(request, customer_pk):
    """Record one payment across several of a customer's invoices (FR-BIL-4)."""
    customer = access.get_or_404(
        request.actor, "customers.view", Customer.objects.all(), pk=customer_pk
    )
    open_invoices = list(
        customer.invoices.filter(status__in=OPEN)
        .exclude(kind=Invoice.Kind.CREDIT_NOTE)
        .order_by("due_date")
    )
    form = PaymentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        allocations = {}
        try:
            for inv in open_invoices:
                raw = request.POST.get(f"apply-{inv.pk}", "").strip()
                if raw:
                    allocations[inv] = Decimal(raw)
            services.record_payment(
                customer,
                amount=d["amount"],
                received_on=d["received_on"],
                method=d["method"],
                reference=d["reference"],
                notes=d["notes"],
                allocations=allocations,
                user=request.user,
            )
        except (ValidationError, InvalidOperation) as exc:
            form.add_error(None, getattr(exc, "messages", ["Check the amounts applied."]))
        else:
            messages.success(request, "Payment recorded.")
            return redirect(customer)
    return render(
        request,
        "billing/payment.html",
        {
            "customer": customer,
            "form": form,
            "invoices": open_invoices,
            "credit": services.customer_credit(customer),
        },
    )


@permission_required("billing.manage_templates")
def template_list(request):
    return render(request, "billing/templates.html", {"templates": InvoiceTemplate.objects.all()})


@permission_required("billing.manage_templates")
def template_edit(request, pk=None):
    obj = InvoiceTemplate.objects.filter(pk=pk).first() if pk else InvoiceTemplate()
    if pk and obj is None:
        raise Http404
    before = audit.snapshot(obj) if pk else None
    form = TemplateForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        problems = templating.validate(form.cleaned_data["body"])
        if problems:
            form.add_error("body", problems)
        else:
            template = save_audited(form.save(commit=False), before=before)
            if template.is_default:
                InvoiceTemplate.objects.exclude(pk=template.pk).update(is_default=False)
            return redirect("billing:templates")
    return render(
        request,
        "core/template_editor.html",
        {
            "form": form,
            "title": f"Edit {obj}" if pk else "New invoice template",
            "back": "billing:templates",
            "back_label": "Invoice templates",
            **placeholders.editor_context("invoice"),
            **(
                {
                    "obj": obj,
                    "archive_url": reverse("billing:template_archive", args=[pk]),
                    "delete_url": reverse("billing:template_delete", args=[pk]),
                }
                if pk
                else {}
            ),
        },
    )


def _template_refusal(template) -> str:
    if template.is_default:
        return "This is the default template. Make another template the default first."
    return ""


@permission_required("billing.manage_templates")
@require_POST
def template_archive(request, pk):
    template = get_object_or_404(InvoiceTemplate, pk=pk)
    return archive.archive_view(
        request,
        template,
        back=reverse("billing:template_edit", args=[pk]),
        refuse=_template_refusal(template),
    )


@permission_required("billing.manage_templates")
def template_delete(request, pk):
    template = get_object_or_404(InvoiceTemplate, pk=pk)
    return archive.delete_view(
        request,
        template,
        archive_url=reverse("billing:template_archive", args=[pk]),
        done_url=reverse("billing:templates"),
        back=reverse("billing:template_edit", args=[pk]),
        refuse=_template_refusal(template),
    )
