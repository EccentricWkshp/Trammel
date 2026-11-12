"""Settings, Online payments: Stripe's key and PayPal's app, switching each on,
testing them, and checking for payments now; and making an invoice a new
payment page."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from trammel.billing.models import OPEN, Invoice
from trammel.core import access, audit, secrets
from trammel.core.web import permission_required
from trammel.core.widgets import SecretBox

from . import paypal, services, stripe
from .errors import ProviderError
from .models import PayLink, PaymentsSettings, PayPalSettings, Provider, StripeSettings
from .providers import PROVIDERS

PERMISSION = "payments.manage"


class _SecretForm(forms.ModelForm):
    """A provider's settings with one write-only secret, audited by name."""

    secret_field = ""
    secret_key = ""
    secret_label = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if secrets.is_set(self.secret_key):
            self.fields[self.secret_field].help_text += " Saved; leave blank to keep it."

    def secret_given(self) -> str:
        return self.cleaned_data.get(self.secret_field, "").strip()

    def save(self, commit=True):
        before = audit.snapshot(self.instance, fields=self.Meta.fields)
        row = super().save(commit=commit)
        changed = [f.replace("_", " ") for f in self.changed_data if f in self.Meta.fields]
        if secret := self.secret_given():
            secrets.put(self.secret_key, secret)
            changed.append(self.secret_label)
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


class StripeForm(_SecretForm):
    secret_field, secret_key, secret_label = "stripe_key", stripe.KEY, "Stripe secret key"
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


class PayPalForm(_SecretForm):
    secret_field, secret_key, secret_label = "client_secret", paypal.SECRET_KEY, "PayPal secret"
    client_secret = forms.CharField(
        label="Secret",
        required=False,
        widget=SecretBox(),
        help_text="The app's secret, from the same page as its client ID.",
    )

    class Meta:
        model = PayPalSettings
        fields = ["enabled", "sandbox", "client_id"]

    def clean_client_id(self):
        return self.cleaned_data.get("client_id", "").strip()

    def clean(self):
        data = super().clean()
        if data.get("enabled"):
            if not data.get("client_id"):
                self.add_error("client_id", "Enter the app's client ID to take payments.")
            if not (self.secret_given() or secrets.is_set(paypal.SECRET_KEY)):
                self.add_error("client_secret", "Enter the app's secret to take payments.")
        return data


class PrimaryForm(forms.ModelForm):
    class Meta:
        model = PaymentsSettings
        fields = ["primary"]


def status() -> dict:
    """What the settings page and the API show: Stripe as before, and PayPal."""
    stripe_on, paypal_on = PROVIDERS[Provider.STRIPE], PROVIDERS[Provider.PAYPAL]
    taking = {p.key for p in services.providers()}
    key_set = stripe_on.credentials()
    return {
        "row": StripeSettings.load(),
        "key_set": key_set,
        "test_mode": key_set and stripe.test_mode(),
        "active": services.active(),
        "primary": PaymentsSettings.load().primary,
        "stripe_active": Provider.STRIPE in taking,
        "links": PayLink.objects.filter(active=True, provider=Provider.STRIPE).count(),
        "paypal": {
            "row": PayPalSettings.load(),
            "credentials": paypal_on.credentials(),
            "secret_set": secrets.is_set(paypal.SECRET_KEY),
            "active": Provider.PAYPAL in taking,
            "links": PayLink.objects.filter(active=True, provider=Provider.PAYPAL).count(),
        },
    }


def _page(request, stripe_form=None, paypal_form=None):
    return render(
        request,
        "payments/settings.html",
        {
            "form": stripe_form or StripeForm(instance=StripeSettings.load(), prefix="stripe"),
            "paypal_form": paypal_form
            or PayPalForm(instance=PayPalSettings.load(), prefix="paypal"),
            "permissions": stripe.PERMISSIONS,
            "primary_form": PrimaryForm(instance=PaymentsSettings.load()),
            **status(),
        },
    )


@permission_required(PERMISSION)
def settings_page(request):
    if request.method == "POST":
        form = StripeForm(request.POST, instance=StripeSettings.load(), prefix="stripe")
        if form.is_valid():
            form.save()
            messages.success(request, "Saved.")
            return redirect("payments:settings")
        return _page(request, stripe_form=form)
    return _page(request)


@permission_required(PERMISSION)
@require_POST
def paypal_settings(request):
    form = PayPalForm(request.POST, instance=PayPalSettings.load(), prefix="paypal")
    if form.is_valid():
        form.save()
        messages.success(request, "Saved.")
        return redirect("payments:settings")
    return _page(request, paypal_form=form)


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
def test_paypal(request):
    try:
        paypal.check_credentials()
    except paypal.PayPalError as exc:
        messages.error(request, str(exc))
    else:
        sandbox = PayPalSettings.load().sandbox
        mode = "sandbox: no real money moves" if sandbox else "live: real payments"
        messages.success(request, f"PayPal accepted the app's credentials ({mode}).")
    return redirect("payments:settings")


@permission_required(PERMISSION)
@require_POST
def check_now(request):
    if not services.active():
        messages.error(request, "Switch Stripe or PayPal on, with its credentials, first.")
        return redirect("payments:settings")
    result = services.run_check()
    recorded, made = result.get("recorded", 0), result.get("links_made", 0)
    summary = (
        f"{recorded} payment{'s' if recorded != 1 else ''} recorded, "
        f"{made} payment page{'s' if made != 1 else ''} made."
    )
    if result.get("error"):
        messages.error(request, f"The check found a problem: {result['error']} ({summary})")
    else:
        messages.success(request, f"Checked: {summary}")
    return redirect("payments:settings")


@permission_required(PERMISSION)
@require_POST
def new_link(request, pk):
    """Make the invoice a fresh payment page at one provider (its old one stops
    working)."""
    invoice = get_object_or_404(
        access.scoped(request.actor, "billing.view", Invoice.objects.all()), pk=pk
    )
    taking = {p.key: p for p in services.providers()}
    chosen = services.provider_for(invoice)
    provider = request.POST.get("provider") or (chosen.key if chosen else Provider.STRIPE)
    if provider not in taking:
        messages.error(request, "That way of paying online isn't on.")
    elif not (invoice.status in OPEN and invoice.balance > 0 and not invoice.is_credit_note):
        messages.error(request, "Only an open invoice with a balance can be paid online.")
    else:
        try:
            services.make_link(invoice, provider)
        except ProviderError as exc:
            messages.error(request, str(exc))
        else:
            label = taking[provider].label
            audit.record(
                "payments.new_link",
                invoice,
                summary=f"Made a new {label} payment page for {invoice}",
            )
            messages.success(
                request, f"A new {label} payment page is ready; the old one no longer works."
            )
    return redirect(invoice.get_absolute_url())


@permission_required(PERMISSION)
@require_POST
def primary(request):
    """Which provider customers are offered, unless their record or the invoice
    says otherwise."""
    row = PaymentsSettings.load()
    before = audit.snapshot(row, fields=["primary"])
    form = PrimaryForm(request.POST, instance=row)
    if form.is_valid():
        row = form.save()
        old, new = audit.diff(before, audit.snapshot(row, fields=["primary"]))
        if new:
            audit.record(
                "update",
                row,
                summary=f"Made {row.get_primary_display()} the primary online payment provider",
                before=old,
                after=new,
            )
            services._follow(services._payable().values_list("pk", flat=True))
        messages.success(request, "Saved. Open invoices follow within a minute.")
    else:
        messages.error(request, "Choose Stripe or PayPal.")
    return redirect("payments:settings")


def _choice(request) -> str | None:
    value = request.POST.get("provider", "")
    if value and value not in PROVIDERS:
        return "bad"
    return value or None


@permission_required(PERMISSION)
@require_POST
def customer_choice(request, pk):
    """Offer a customer's invoices through another provider than the primary."""
    from trammel.customers.models import Customer

    customer = access.get_or_404(request.actor, "customers.view", Customer.objects.all(), pk=pk)
    provider = _choice(request)
    if provider == "bad":
        messages.error(request, "Choose Stripe, PayPal, or the shop's primary.")
    else:
        services.choose_for_customer(customer, provider)
        messages.success(request, "Saved. Their open invoices follow within a minute.")
    return redirect(customer.get_absolute_url())


@permission_required(PERMISSION)
@require_POST
def invoice_choice(request, pk):
    """Offer one invoice through a particular provider."""
    invoice = get_object_or_404(
        access.scoped(request.actor, "billing.view", Invoice.objects.all()), pk=pk
    )
    provider = _choice(request)
    if provider == "bad":
        messages.error(request, "Choose Stripe, PayPal, or as its customer is.")
    else:
        services.choose_for_invoice(invoice, provider)
        messages.success(request, "Saved. Its payment page follows within a minute.")
    return redirect(invoice.get_absolute_url())
