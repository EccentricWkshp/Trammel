"""Settings, Online payments: Stripe's key, switching it on, testing it, and
checking for payments now; and making an invoice a new payment link."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from trammel.billing.models import Invoice
from trammel.core import access, audit, secrets
from trammel.core.web import permission_required
from trammel.core.widgets import SecretBox

from . import services, stripe
from .models import StripeSettings

PERMISSION = "payments.manage"


class StripeForm(forms.ModelForm):
    stripe_key = forms.CharField(
        label="Stripe secret key",
        required=False,
        widget=SecretBox(),
        help_text="A restricted key (rk_…) with the permissions listed on this page, or the "
        "secret key (sk_…). A test key (…_test_…) takes no real money.",
    )

    class Meta:
        model = StripeSettings
        fields = ["enabled"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if secrets.is_set(stripe.KEY):
            self.fields["stripe_key"].help_text += " Saved; leave blank to keep it."

    def clean_stripe_key(self):
        value = self.cleaned_data.get("stripe_key", "").strip()
        if value and not value.startswith(("sk_", "rk_")):
            raise forms.ValidationError(
                "That isn't a Stripe secret key: it starts with sk_ or rk_ (not pk_, the "
                "publishable one)."
            )
        return value

    def clean(self):
        data = super().clean()
        if data.get("enabled") and not (data.get("stripe_key") or secrets.is_set(stripe.KEY)):
            self.add_error("stripe_key", "Enter the key to take payments.")
        return data

    def save(self, commit=True):
        before = audit.snapshot(self.instance, fields=self.Meta.fields)
        row = super().save(commit=commit)
        changed = [f.replace("_", " ") for f in self.changed_data if f in self.Meta.fields]
        key = self.cleaned_data.get("stripe_key", "")
        if key:
            secrets.put(stripe.KEY, key)
            changed.append("Stripe secret key")
        if changed:
            old, new = audit.diff(before, audit.snapshot(row, fields=self.Meta.fields))
            audit.record(
                "update",
                row,
                summary=f"Changed the online payments settings: {', '.join(changed)}",
                before=old,
                after=new,
            )
        return row


def status() -> dict:
    """What the settings page and the API show."""
    row = StripeSettings.load()
    key_set = secrets.is_set(stripe.KEY)
    return {
        "row": row,
        "key_set": key_set,
        "test_mode": key_set and stripe.test_mode(),
        "active": services.active(),
        "links": services.PayLink.objects.filter(active=True).count(),
    }


@permission_required(PERMISSION)
def settings_page(request):
    row = StripeSettings.load()
    form = StripeForm(request.POST or None, instance=row)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Saved.")
        return redirect("payments:settings")
    return render(
        request,
        "payments/settings.html",
        {"form": form, "permissions": stripe.PERMISSIONS, **status()},
    )


@permission_required(PERMISSION)
@require_POST
def test_key(request):
    try:
        stripe.check_key()
    except stripe.StripeError as exc:
        messages.error(request, str(exc))
    else:
        mode = "test mode: no real money moves" if stripe.test_mode() else "live: real payments"
        messages.success(request, f"Stripe accepted the key ({mode}).")
    return redirect("payments:settings")


@permission_required(PERMISSION)
@require_POST
def check_now(request):
    if not services.active():
        messages.error(request, "Switch online payments on, with a key, first.")
        return redirect("payments:settings")
    result = services.run_check()
    if result.get("error"):
        messages.error(request, f"The check failed: {result['error']}")
    else:
        recorded, made = result["recorded"], result["links_made"]
        messages.success(
            request,
            f"Checked: {recorded} payment{'s' if recorded != 1 else ''} recorded, "
            f"{made} link{'s' if made != 1 else ''} made.",
        )
    return redirect("payments:settings")


@permission_required(PERMISSION)
@require_POST
def new_link(request, pk):
    """Make the invoice a fresh payment link (the old one stops working)."""
    invoice = get_object_or_404(
        access.scoped(request.actor, "billing.view", Invoice.objects.all()), pk=pk
    )
    if not services.active():
        messages.error(request, "Online payments aren't on.")
    elif not (invoice.status in services.OPEN and invoice.balance > 0):
        messages.error(request, "Only an open invoice with a balance can be paid online.")
    else:
        try:
            services.make_link(invoice)
        except stripe.StripeError as exc:
            messages.error(request, str(exc))
        else:
            audit.record(
                "payments.new_link", invoice, summary=f"Made a new payment link for {invoice}"
            )
            messages.success(request, "A new payment link is ready; the old one no longer works.")
    return redirect(invoice.get_absolute_url())
