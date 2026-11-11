from __future__ import annotations

import datetime as dt
import json

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from trammel.core import access, audit, secrets, tasks
from trammel.core.services import save_audited
from trammel.core.web import paginate, permission_required
from trammel.customers.models import Customer

from . import services, shopify
from .models import ImportedOrder, ShopifySettings

#: The most a webhook body may be; a Shopify order is a few kilobytes.
MAX_WEBHOOK_BYTES = 2 * 1024 * 1024


@permission_required("imports.view")
def order_list(request):
    config = ShopifySettings.load()
    pending = ImportedOrder.objects.filter(status=ImportedOrder.Status.PENDING)
    imported = ImportedOrder.objects.filter(status=ImportedOrder.Status.IMPORTED).select_related(
        "invoice", "customer", "job"
    )
    today = timezone.localdate()
    return render(
        request,
        "imports/list.html",
        {
            "config": config,
            "connected": config.enabled and bool(config.shop_domain),
            "pending": pending,
            "page": paginate(request, imported, 50),
            "needs_attention": imported.exclude(problem=""),
            "can_run": access.has_perm(request.actor, "imports.run"),
            "can_manage": access.has_perm(request.actor, "imports.manage"),
            "start": (today - dt.timedelta(days=7)).isoformat(),
            "end": today.isoformat(),
            "customers": Customer.objects.filter(is_internal=False, is_placeholder=False)
            .order_by("name")
            .only("pk", "name", "email"),
        },
    )


@permission_required("imports.run")
@require_POST
def fetch(request):
    """Fetch and bring in a date range of Shopify orders (FR-IMP-1)."""
    config = ShopifySettings.load()
    if not config.enabled:
        messages.error(request, "Connect Shopify in Settings, Online stores, first.")
        return redirect("imports:list")
    try:
        start = dt.date.fromisoformat(request.POST.get("start", ""))
        end = dt.date.fromisoformat(request.POST.get("end", ""))
    except ValueError:
        messages.error(request, "Choose the first and last day to fetch.")
        return redirect("imports:list")
    if end < start:
        start, end = end, start
    try:
        raw = shopify.fetch(config, start, end)
    except (shopify.ShopifyError, ValidationError) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        return redirect("imports:list")
    result = services.import_orders(
        [shopify.read(order) for order in raw], "shopify", user=request.user
    )
    audit.record(
        "import.fetch",
        entity_type="imports.shopifysettings",
        entity_id="1",
        summary=f"Fetched {len(raw)} Shopify order(s) from {start} to {end}",
    )
    if not raw:
        messages.info(request, f"Shopify has no orders from {start:%m/%d/%Y} to {end:%m/%d/%Y}.")
    else:
        (messages.warning if result.errors or result.pending else messages.success)(
            request, result.summary("Shopify")
        )
        for error in result.errors[:10]:
            messages.error(request, error)
    return redirect("imports:list")


def _waiting(request, pk) -> ImportedOrder:
    return get_object_or_404(ImportedOrder, pk=pk, status=ImportedOrder.Status.PENDING)


class NewCustomerForm(forms.Form):
    name = forms.CharField(max_length=200, required=False)
    company = forms.CharField(max_length=200, required=False)
    email = forms.EmailField(required=False)
    phone = forms.CharField(max_length=40, required=False)
    line1 = forms.CharField(label="Address", max_length=200, required=False)
    line2 = forms.CharField(label="Address line 2", max_length=200, required=False)
    city = forms.CharField(max_length=100, required=False)
    region = forms.CharField(label="State", max_length=100, required=False)
    postal_code = forms.CharField(label="ZIP code", max_length=20, required=False)
    country = forms.CharField(max_length=2, required=False, initial="US")


@permission_required("imports.run")
def complete(request, pk):
    """Give an order waiting for a customer its customer, and make its sale."""
    row = _waiting(request, pk)
    hints = row.hints
    form = NewCustomerForm(
        request.POST or None,
        initial={
            "region": hints["region"],
            "country": hints["country"] or "US",
            "postal_code": hints["postal_code"],
            "city": hints["city"],
        },
    )
    if request.method == "POST" and form.is_valid():
        chosen = None
        if pk_ := request.POST.get("customer"):
            chosen = access.get_or_404(
                request.actor, "customers.view", Customer.objects.all(), pk=pk_
            )
        try:
            invoice = services.complete(
                row,
                customer=chosen,
                new=None if chosen else form.cleaned_data,
                user=request.user,
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, f"{row} is now sale {invoice.number}.")
            return redirect(invoice)
    return render(
        request,
        "imports/complete.html",
        {
            "row": row,
            "form": form,
            "lines": row.payload.get("lines") or [],
            "customers": Customer.objects.filter(is_internal=False, is_placeholder=False)
            .order_by("name")
            .only("pk", "name", "email"),
        },
    )


@permission_required("imports.run")
@require_POST
def discard(request, pk):
    row = _waiting(request, pk)
    services.discard(row)
    messages.success(request, f"{row} discarded. Fetching its dates again brings it back.")
    return redirect("imports:list")


@permission_required("imports.run")
@require_POST
def make_job(request, pk):
    row = get_object_or_404(ImportedOrder, pk=pk)
    access.require(request.actor, "jobs.create")
    try:
        job = services.make_job(row, user=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(row.invoice or "imports:list")
    messages.success(
        request, f"{job.number} is approved and ready for a work order from its quote."
    )
    return redirect(job)


# ---------------------------------------------------------------------------
# Connecting Shopify
# ---------------------------------------------------------------------------


class SecretBox(forms.TextInput):
    """A write-only box for a key pasted from Shopify: plain text, so browsers and
    password managers don't take it for a sign-in form, and empty on every page,
    even when the form comes back with an error."""

    def format_value(self, value):
        return None


def _secret_box():
    return SecretBox(
        attrs={
            "autocomplete": "off",
            "spellcheck": "false",
            "autocapitalize": "off",
            "class": "secret-box",
            "data-1p-ignore": "",
            "data-lpignore": "true",
            "data-bwignore": "",
            "data-form-type": "other",
        }
    )


class ShopifyForm(forms.ModelForm):
    token = forms.CharField(
        label="Admin API access token",
        required=False,
        widget=_secret_box(),
        help_text="The shpat_ token shown once when the custom app's API access is set up.",
    )
    api_secret = forms.CharField(
        label="API secret key",
        required=False,
        widget=_secret_box(),
        help_text="Only for webhooks: Shopify signs them with it.",
    )
    client_secret = forms.CharField(
        label="Client secret",
        required=False,
        widget=_secret_box(),
        help_text="From the app in the Dev Dashboard; it also signs webhooks.",
    )

    class Meta:
        model = ShopifySettings
        fields = ["enabled", "shop_domain", "auth_mode", "client_id", "webhooks_enabled"]
        widgets = {"auth_mode": forms.RadioSelect}

    SECRETS = {
        "token": shopify.TOKEN_KEY,
        "api_secret": shopify.API_SECRET_KEY,
        "client_secret": shopify.CLIENT_SECRET_KEY,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, key in self.SECRETS.items():
            if secrets.is_set(key):
                self.fields[name].help_text += " Saved; leave blank to keep it."

    def clean_shop_domain(self):
        value = self.cleaned_data.get("shop_domain", "")
        return shopify.check_domain(value) if value.strip() else ""

    def clean(self):
        data = super().clean()
        if data.get("enabled") and not data.get("shop_domain"):
            self.add_error("shop_domain", "Enter the store's address to connect it.")
        return data


@permission_required("imports.manage")
def settings_view(request):
    config = ShopifySettings.load()
    before = audit.snapshot(config)
    form = ShopifyForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        changed = form.has_changed()
        save_audited(form.save(commit=False), before=before, summary="Edited Shopify settings")
        for name, key in ShopifyForm.SECRETS.items():
            if value := form.cleaned_data.get(name, "").strip():
                secrets.put(key, value)
                changed = True
                audit.record(
                    "credential.set",
                    entity_type="core.credential",
                    entity_id=key,
                    summary=f"Changed the Shopify {form.fields[name].label.lower()}",
                )
        if changed:
            shopify.forget_token()
        messages.success(request, "Shopify settings saved.")
        return redirect("imports:settings")
    return render(
        request,
        "imports/settings.html",
        {
            "form": form,
            "config": config,
            "webhook_url": request.build_absolute_uri(reverse("imports:shopify_webhook"))
            if config.webhooks_enabled
            else "",
        },
    )


@permission_required("imports.manage")
@require_POST
def test_connection(request):
    """Ask Shopify for one day of orders, to show the connection works."""
    config = ShopifySettings.load()
    try:
        today = timezone.localdate()
        orders = shopify.fetch(config, today, today)
    except (shopify.ShopifyError, ValidationError) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
    else:
        messages.success(
            request, f"Connected to {config.shop_domain}: {len(orders)} order(s) today."
        )
    return redirect("imports:settings")


# ---------------------------------------------------------------------------
# Shopify webhooks (FR-IMP-4)
# ---------------------------------------------------------------------------


@csrf_exempt
@require_POST
def shopify_webhook(request):
    """orders/create, orders/updated and orders/fulfilled from Shopify.

    Not found unless Shopify and its webhooks are switched on and a secret is
    there to check signatures with. The signature (HMAC of the exact body) is
    the only credential Shopify can present; nothing is read before it checks
    out. The order is queued, so Shopify gets its answer at once.
    """
    config = ShopifySettings.load()
    if not (config.enabled and config.webhooks_enabled and shopify.webhook_secret(config)):
        raise Http404
    length = int(request.META.get("CONTENT_LENGTH") or 0)
    if length > MAX_WEBHOOK_BYTES:
        return HttpResponse(status=413)
    body = request.body
    if not shopify.verify_webhook(config, body, request.headers.get("X-Shopify-Hmac-Sha256", "")):
        return HttpResponse(status=401)
    shop = (request.headers.get("X-Shopify-Shop-Domain") or "").lower()
    if shop != shopify.normalize_domain(config.shop_domain):
        return HttpResponse(status=401)
    try:
        order = json.loads(body)
    except ValueError:
        return HttpResponse(status=400)
    if not isinstance(order, dict) or not order.get("id"):
        return HttpResponse(status=200)  # nothing to import; don't make Shopify retry
    tasks.enqueue("imports.shopify_order", {"order": order})
    return HttpResponse(status=200)
