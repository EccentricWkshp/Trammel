from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_POST

from trammel.core import access, audit, placeholders, templating
from trammel.core.middleware import client_ip
from trammel.core.services import save_audited
from trammel.core.web import permission_required
from trammel.jobs.models import Job

from . import services
from .models import AgreementSettings, AgreementTemplate, CustomerAgreement


class TemplateForm(forms.ModelForm):
    body = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 18}),
        help_text="Plain text. Placeholders are filled in when the agreement is issued, e.g. "
        "{{ customer.name }}, "
        "{{ job.number }}, {{ payment_terms_days }}, {{ deposit_percent }}.",
    )

    class Meta:
        model = AgreementTemplate
        fields = ["name", "is_default", "is_active"]


class AcceptForm(forms.Form):
    accepted_by_name = forms.CharField(label="Accepted by", max_length=150)
    accepted_at = forms.DateTimeField(
        label="When",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        initial=timezone.now,
    )
    method = forms.ChoiceField(
        choices=[
            (CustomerAgreement.Method.MANUAL, "Recorded by staff"),
            (CustomerAgreement.Method.UPLOAD, "Signed document (upload it below)"),
        ]
    )
    how = forms.ChoiceField(choices=CustomerAgreement.How.choices, required=False)
    document = forms.FileField(label="Signed document", required=False)
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)


class SettingsForm(forms.ModelForm):
    class Meta:
        model = AgreementSettings
        fields = ["required", "manual_enabled", "upload_enabled", "click_enabled"]


@permission_required("agreements.view")
def template_list(request):
    return render(
        request,
        "agreements/templates.html",
        {
            "templates": AgreementTemplate.objects.prefetch_related("versions"),
            "settings": AgreementSettings.load(),
        },
    )


@permission_required("agreements.manage")
def template_edit(request, pk=None):
    template = get_object_or_404(AgreementTemplate, pk=pk) if pk else AgreementTemplate()
    current = template.current if pk else None
    form = TemplateForm(
        request.POST or None, instance=template, initial={"body": current.body if current else ""}
    )
    if request.method == "POST" and form.is_valid():
        try:
            version = services.save_template(
                form.save(commit=False), form.cleaned_data["body"], user=request.user
            )
        except ValidationError as exc:
            form.add_error("body", exc)
        else:
            messages.success(request, f"{template} saved as version {version.version}.")
            return redirect("agreements:templates")
    return render(
        request,
        "core/template_editor.html",
        {
            "form": form,
            "title": f"Edit {template}" if pk else "New agreement template",
            "back": "agreements:templates",
            "back_label": "Agreements",
            **placeholders.editor_context("agreement"),
        },
    )


@permission_required("agreements.manage")
def settings_edit(request):
    obj = AgreementSettings.load()
    before = audit.snapshot(obj)
    form = SettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        save_audited(form.save(commit=False), before=before, summary="Edited agreement settings")
        messages.success(request, "Agreement settings saved.")
        return redirect("agreements:templates")
    return render(
        request,
        "core/settings_form.html",
        {"form": form, "title": "Agreement settings", "back": "agreements:templates"},
    )


def _job(request, perm, pk):
    return access.get_or_404(request.actor, perm, Job.objects.select_related("customer"), pk=pk)


@permission_required("agreements.manage")
@require_POST
def issue(request, job_pk):
    job = _job(request, "agreements.manage", job_pk)
    try:
        services.issue(job)
    except (ValidationError, templating.TemplateProblem) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
    else:
        messages.success(request, "Agreement issued.")
    return redirect(job)


@permission_required("agreements.view")
def detail(request, pk):
    agreement = access.get_or_404(
        request.actor,
        "agreements.view",
        CustomerAgreement.objects.select_related("job__customer"),
        pk=pk,
    )
    return render(
        request,
        "agreements/detail.html",
        {
            "agreement": agreement,
            "pdf": templating.pdf_available(),
            "can_manage": access.has_perm(request.actor, "agreements.manage", agreement),
        },
    )


@permission_required("agreements.view")
def pdf(request, pk):
    agreement = access.get_or_404(
        request.actor,
        "agreements.view",
        CustomerAgreement.objects.select_related("job__customer"),
        pk=pk,
    )
    from django.template.loader import render_to_string

    html = render_to_string(
        "agreements/print.html", {"agreement": agreement, "business": request.business}
    )
    try:
        data = templating.render_pdf(html)
    except templating.PdfUnavailable as exc:
        messages.error(request, str(exc))
        return redirect(agreement)
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = content_disposition_header(
        True, f"agreement-{agreement.job.number}.pdf"
    )
    return response


@permission_required("agreements.manage")
def accept(request, pk):
    agreement = access.get_or_404(
        request.actor, "agreements.manage", CustomerAgreement.objects.select_related("job"), pk=pk
    )
    form = AcceptForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        evidence = None
        try:
            if data["document"]:
                from trammel.files.services import upload

                evidence = upload(
                    data["document"],
                    name=data["document"].name,
                    title=f"Signed agreement {agreement.job.number}",
                    attach_to=agreement.job,
                    user=request.user,
                ).document
            services.accept(
                agreement,
                method=data["method"],
                accepted_by_name=data["accepted_by_name"],
                accepted_at=data["accepted_at"],
                how=data["how"],
                notes=data["notes"],
                evidence=evidence,
                ip=client_ip(request),
                user=request.user,
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Acceptance recorded.")
            return redirect(agreement.job)
    return render(request, "agreements/accept.html", {"form": form, "agreement": agreement})


@permission_required("agreements.manage")
@require_POST
def waive(request, job_pk):
    job = _job(request, "agreements.manage", job_pk)
    try:
        if request.POST.get("undo"):
            services.unwaive(job)
        else:
            services.waive(job, reason=request.POST.get("reason", ""))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(job)
