from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from trammel.core import access, audit, placeholders, secrets, templating
from trammel.core.services import save_audited
from trammel.core.web import admin_required, paginate, permission_required
from trammel.core.widgets import MultiSelectDropdown

from . import services
from .events import EVENTS, listed
from .models import EmailLog, EmailTemplate, SmtpSettings


class SmtpForm(forms.ModelForm):
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Stored encrypted. Leave blank to keep the current password.",
    )
    clear_password = forms.BooleanField(required=False, label="Remove the stored password")

    class Meta:
        model = SmtpSettings
        fields = [
            "host",
            "port",
            "security",
            "username",
            "from_address",
            "from_name",
            "staff_recipients",
            "overdue_reminder_days",
        ]
        widgets = {"staff_recipients": MultiSelectDropdown(placeholder="Every system admin")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from trammel.accounts.models import User

        field = self.fields["staff_recipients"]
        chosen = self.instance.staff_recipients.all() if self.instance.pk else User.objects.none()
        field.queryset = (
            (User.objects.filter(is_active=True).exclude(email="") | chosen)
            .distinct()
            .order_by("full_name", "email")
        )
        field.label_from_instance = lambda u: f"{u} <{u.email}>" if str(u) != u.email else u.email


class TemplateForm(forms.ModelForm):
    class Meta:
        model = EmailTemplate
        fields = ["enabled", "subject", "body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 12, "class": "code"})}

    def clean(self):
        data = super().clean()
        for field in ("subject", "body"):
            for problem in templating.validate(data.get(field, ""), html=False):
                self.add_error(field, problem)
        return data


@admin_required
def smtp(request):
    obj = SmtpSettings.load()
    before = audit.snapshot(obj)
    form = SmtpForm(request.POST or None, instance=obj)
    if request.method == "POST" and "test_to" in request.POST:
        try:
            services.send_test(request.POST["test_to"], user=request.user)
        except Exception as exc:  # noqa: BLE001 — report the server's own error message
            messages.error(request, f"The test email failed: {exc}")
        else:
            messages.success(request, f"Test email sent to {request.POST['test_to']}.")
        return redirect("notifications:smtp")
    if request.method == "POST" and form.is_valid():
        save_audited(form.save(commit=False), before=before, summary="Edited email settings")
        form.save_m2m()
        if form.cleaned_data["clear_password"]:
            secrets.clear(services.PASSWORD_KEY)
            audit.record(
                "credential.clear",
                entity_type="core.credential",
                entity_id=services.PASSWORD_KEY,
                summary="Removed the SMTP password",
            )
        elif form.cleaned_data["password"]:
            secrets.put(services.PASSWORD_KEY, form.cleaned_data["password"])
            audit.record(
                "credential.set",
                entity_type="core.credential",
                entity_id=services.PASSWORD_KEY,
                summary="Changed the SMTP password",
            )
        messages.success(request, "Email settings saved.")
        return redirect("notifications:smtp")
    return render(
        request,
        "notifications/smtp.html",
        {
            "form": form,
            "password_set": secrets.is_set(services.PASSWORD_KEY),
            "templates": [(e, services.template_for(e.key)) for e in listed()],
        },
    )


@admin_required
@require_POST
def template_toggle(request, event):
    if event in EVENTS:
        tpl = services.template_for(event)
        before = audit.snapshot(tpl)
        tpl.enabled = request.POST.get("enabled") == "1"
        save_audited(
            tpl,
            before=before,
            summary=f"{'Turned on' if tpl.enabled else 'Turned off'} the "
            f"“{EVENTS[event].label}” email",
        )
    return redirect(f"{reverse('notifications:smtp')}#events-heading")


@admin_required
def template_edit(request, event):
    if event not in EVENTS:
        return redirect("notifications:smtp")
    tpl = services.template_for(event)
    before = audit.snapshot(tpl)
    form = TemplateForm(request.POST or None, instance=tpl)
    if request.method == "POST" and form.is_valid():
        save_audited(form.save(commit=False), before=before)
        return redirect("notifications:smtp")
    return render(
        request,
        "core/template_editor.html",
        {
            "form": form,
            "title": f"Email: {EVENTS[event].label}",
            "intro": f"Goes to {EVENTS[event].audience_label}.",
            "back": "notifications:smtp",
            "back_label": "Email",
            **placeholders.editor_context(f"email:{event}"),
        },
    )


@admin_required
def email_log(request):
    return render(
        request, "notifications/log.html", {"page": paginate(request, EmailLog.objects.all(), 100)}
    )


def _back(request, obj):
    return redirect(obj.get_absolute_url())


@permission_required("quotes.edit")
@require_POST
def email_quote(request, pk):
    from trammel.quotes.models import QuoteRevision
    from trammel.quotes.services import document_context

    rev = access.get_or_404(
        request.actor,
        "quotes.edit",
        QuoteRevision.objects.select_related("quote__job__customer"),
        pk=pk,
    )
    to = request.POST.get("to", "").strip() or rev.quote.job.customer.email
    if not to:
        messages.error(request, "The customer has no email address.")
        return _back(request, rev)
    entry = services.notify(
        "quote_sent",
        to=[to],
        context=document_context(rev),
        attachment={"kind": "quote", "id": str(rev.pk)},
        entity=rev.quote,
    )
    _report(request, entry, to)
    return _back(request, rev)


@permission_required("billing.view")
@require_POST
def email_invoice(request, pk):
    from trammel.billing.models import Invoice
    from trammel.billing.services import document_context

    inv = access.get_or_404(
        request.actor, "billing.view", Invoice.objects.select_related("customer"), pk=pk
    )
    if inv.is_draft:
        messages.error(request, "Issue the invoice before emailing it.")
        return _back(request, inv)
    to = request.POST.get("to", "").strip() or inv.customer.email
    if not to:
        messages.error(request, "The customer has no email address.")
        return _back(request, inv)
    entry = services.notify(
        "invoice_issued",
        to=[to],
        context=document_context(inv),
        attachment={"kind": "invoice", "id": str(inv.pk)},
        entity=inv,
    )
    _report(request, entry, to)
    return _back(request, inv)


def _report(request, entry, to):
    if entry is None or entry.status == EmailLog.Status.SKIPPED:
        messages.warning(request, f"Not sent: {entry.detail if entry else 'no recipient'}")
    elif entry.status == EmailLog.Status.FAILED:
        messages.error(request, f"Not sent: {entry.detail}")
    else:
        messages.success(request, f"Email to {to} queued.")
