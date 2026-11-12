"""Online payments over the API (FR-API-1): their settings and status, checking
for payments now, and an invoice's payment link and what was paid through it."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from uuid import UUID

from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from ninja import Router, Schema

from trammel.billing.models import OPEN, Invoice
from trammel.core import access, audit

from . import services
from .errors import ProviderError
from .models import OnlinePayment, PayPalSettings, StripeSettings

router = Router(tags=["online payments"])

PERMISSION = "payments.manage"


class PayPalStatusOut(Schema):
    enabled: bool
    sandbox: bool
    client_id: str
    #: The secret is saved (it's never returned).
    secret_set: bool
    #: Taking payments through PayPal now.
    active: bool
    #: Open invoices with a PayPal invoice to pay.
    links: int
    last_check_at: dt.datetime | None
    last_error: str
    last_error_at: dt.datetime | None


class PaymentsStatusOut(Schema):
    #: The provider customers are offered unless their record or the invoice
    #: says otherwise: "stripe" or "paypal".
    primary: str
    #: Stripe switched on in the settings.
    enabled: bool
    #: Taking payments now: on, a key, and outbound traffic allowed.
    active: bool
    key_set: bool
    #: A test key: no real money moves.
    test_mode: bool
    #: Open invoices with a payment link.
    links: int
    last_check_at: dt.datetime | None
    last_error: str
    last_error_at: dt.datetime | None
    paypal: PayPalStatusOut


class PayPalSettingsIn(Schema):
    enabled: bool | None = None
    sandbox: bool | None = None
    client_id: str | None = None
    #: Write-only.
    client_secret: str | None = None


class PaymentsSettingsIn(Schema):
    #: "stripe" or "paypal".
    primary: str | None = None
    #: Stripe on or off.
    enabled: bool | None = None
    #: Write-only: a restricted key (rk_…) or secret key (sk_…).
    stripe_key: str | None = None
    paypal: PayPalSettingsIn | None = None


class CheckOut(Schema):
    recorded: int = 0
    links_made: int = 0
    error: str = ""


class LinkOut(Schema):
    provider: str
    url: str
    #: The provider's own number for it (a PayPal invoice's), or "".
    number: str
    amount: Decimal
    currency: str
    test_mode: bool
    created_at: dt.datetime


class OnlinePaymentOut(Schema):
    id: UUID
    provider: str
    amount: Decimal
    currency: str
    status: str
    reference: str
    email: str
    paid_at: dt.datetime
    #: The payment it was recorded as.
    payment: UUID | None
    test_mode: bool


class InvoicePaymentsOut(Schema):
    invoice: UUID
    balance: Decimal
    #: The one provider it's offered through now ("stripe", "paypal"), or null.
    provider: str | None
    #: Why: "chosen for this invoice", "the customer's choice", "the shop's primary"…
    why: str
    #: Its own choice, or "" (as its customer is).
    chosen: str
    #: The first current payment page, for exactly the balance; null when there's none.
    link: LinkOut | None
    #: Each provider's current page.
    links: list[LinkOut]
    payments: list[OnlinePaymentOut]


def _status() -> dict:
    from .views import status

    found = status()
    row = found["row"]
    return {
        "primary": found["primary"],
        "enabled": row.enabled,
        "active": found["active"],
        "key_set": found["key_set"],
        "test_mode": found["test_mode"],
        "links": found["links"],
        "last_check_at": row.last_check_at,
        "last_error": row.last_error,
        "last_error_at": row.last_error_at,
        "paypal": {
            "enabled": found["paypal"]["row"].enabled,
            "sandbox": found["paypal"]["row"].sandbox,
            "client_id": found["paypal"]["row"].client_id,
            "secret_set": found["paypal"]["secret_set"],
            "active": found["paypal"]["active"],
            "links": found["paypal"]["links"],
            "last_check_at": found["paypal"]["row"].last_check_at,
            "last_error": found["paypal"]["row"].last_error,
            "last_error_at": found["paypal"]["row"].last_error_at,
        },
    }


def _form(form_class, model, data: dict, prefix: str):
    row = model.load()
    form = form_class(
        {f"{prefix}-{k}" if prefix else k: v for k, v in data.items()},
        instance=row,
        prefix=prefix or None,
    )
    if not form.is_valid():
        raise ValidationError({f: list(e) for f, e in form.errors.items()})
    return form


@router.get("/settings", response=PaymentsStatusOut)
def get_settings(request):
    access.require(request.actor, PERMISSION)
    return _status()


@router.patch("/settings", response=PaymentsStatusOut)
def update_settings(request, payload: PaymentsSettingsIn):
    """Checked as on the settings page; secrets are never returned."""
    from .views import PayPalForm, StripeForm

    access.require(request.actor, PERMISSION)
    given = payload.dict(exclude_unset=True, exclude_none=True)
    forms = []
    stripe_given = {k: v for k, v in given.items() if k in ("enabled", "stripe_key")}
    if stripe_given:
        row = StripeSettings.load()
        data = {"enabled": row.enabled, "stripe_key": "", **stripe_given}
        if not data["enabled"]:
            data.pop("enabled")  # an unchecked box, as the form expects
        forms.append(_form(StripeForm, StripeSettings, data, "stripe"))
    if given.get("paypal"):
        row = PayPalSettings.load()
        data = {
            "enabled": row.enabled,
            "sandbox": row.sandbox,
            "client_id": row.client_id,
            "client_secret": "",
            **given["paypal"],
        }
        for box in ("enabled", "sandbox"):
            if not data[box]:
                data.pop(box)
        forms.append(_form(PayPalForm, PayPalSettings, data, "paypal"))
    if given.get("primary"):
        from .models import PaymentsSettings
        from .views import PrimaryForm

        forms.append(_form(PrimaryForm, PaymentsSettings, {"primary": given["primary"]}, ""))
    for form in forms:
        form.save()
    if given.get("primary"):
        services._follow(services._payable().values_list("pk", flat=True))
    return _status()


@router.post("/check", response=CheckOut)
def check(request):
    """Record what was paid and keep the links current, now."""
    access.require(request.actor, PERMISSION)
    if not services.active():
        raise ValidationError("Online payments aren't on.")
    return services.run_check()


def _invoice(request, invoice_id) -> Invoice:
    access.require(request.actor, PERMISSION)
    return get_object_or_404(
        access.scoped(request.actor, "billing.view", Invoice.objects.all()), pk=invoice_id
    )


def _link_out(link) -> dict:
    return {
        "provider": link.provider,
        "url": link.url,
        "number": link.number,
        "amount": link.amount,
        "currency": link.currency,
        "test_mode": link.test_mode,
        "created_at": link.created_at,
    }


def _invoice_out(invoice: Invoice) -> dict:
    link = services.current_link(invoice)
    links = [_link_out(link)] if link else []
    choice = services.choice_of(invoice)
    return {
        "invoice": invoice.pk,
        "balance": invoice.balance,
        "provider": choice["provider"].key if choice["provider"] else None,
        "why": choice["why"],
        "chosen": choice["chosen"],
        "link": links[0] if links else None,
        "links": links,
        "payments": [
            {
                "id": p.pk,
                "provider": p.provider,
                "amount": p.amount,
                "currency": p.currency,
                "status": p.status,
                "reference": p.reference,
                "email": p.email,
                "paid_at": p.paid_at,
                "payment": p.payment_id,
                "test_mode": p.test_mode,
            }
            for p in OnlinePayment.objects.filter(invoice=invoice)
        ],
    }


@router.get("/invoices/{invoice_id}", response=InvoicePaymentsOut)
def invoice_payments(request, invoice_id: UUID):
    return _invoice_out(_invoice(request, invoice_id))


class NewLinkIn(Schema):
    #: "stripe" or "paypal"; by default the one the invoice is offered through.
    provider: str = ""


class ChoiceIn(Schema):
    #: "stripe", "paypal", or null for the default (the customer's, or the primary).
    provider: str | None = None


class CustomerChoiceOut(Schema):
    customer: UUID
    #: Its own choice, or "" for the shop's primary.
    chosen: str
    primary: str


def _check_choice(provider):
    from .providers import PROVIDERS

    if provider and provider not in PROVIDERS:
        raise ValidationError({"provider": ["Choose stripe, paypal, or null."]})


@router.put("/invoices/{invoice_id}", response=InvoicePaymentsOut)
def set_invoice_choice(request, invoice_id: UUID, payload: ChoiceIn):
    """Offer the invoice through a particular provider, or (null) as its customer is."""
    invoice = _invoice(request, invoice_id)
    _check_choice(payload.provider)
    services.choose_for_invoice(invoice, payload.provider or None)
    return _invoice_out(invoice)


def _customer_out(customer) -> dict:
    from .models import CustomerChoice, PaymentsSettings

    found = CustomerChoice.objects.filter(customer=customer).first()
    return {
        "customer": customer.pk,
        "chosen": found.provider if found else "",
        "primary": PaymentsSettings.load().primary,
    }


def _customer(request, customer_id):
    from trammel.customers.models import Customer

    access.require(request.actor, PERMISSION)
    return access.get_or_404(
        request.actor, "customers.view", Customer.objects.all(), pk=customer_id
    )


@router.get("/customers/{customer_id}", response=CustomerChoiceOut)
def customer_choice(request, customer_id: UUID):
    return _customer_out(_customer(request, customer_id))


@router.put("/customers/{customer_id}", response=CustomerChoiceOut)
def set_customer_choice(request, customer_id: UUID, payload: ChoiceIn):
    """Offer the customer's invoices through a provider, or (null) the shop's primary."""
    customer = _customer(request, customer_id)
    _check_choice(payload.provider)
    services.choose_for_customer(customer, payload.provider or None)
    return _customer_out(customer)


@router.post("/invoices/{invoice_id}/link", response=InvoicePaymentsOut)
def new_link(request, invoice_id: UUID, payload: NewLinkIn | None = None):
    """A fresh payment page at one provider for the invoice's balance; its old one
    stops working."""
    invoice = _invoice(request, invoice_id)
    chosen = services.provider_for(invoice)
    provider = (payload.provider if payload else "") or (chosen.key if chosen else "")
    taking = {p.key: p for p in services.providers()}
    if provider not in taking:
        raise ValidationError({"provider": ["That way of paying online isn't on."]})
    if not (invoice.status in OPEN and invoice.balance > 0 and not invoice.is_credit_note):
        raise ValidationError("Only an open invoice with a balance can be paid online.")
    try:
        services.make_link(invoice, provider)
    except ProviderError as exc:
        raise ValidationError(str(exc)) from None
    audit.record(
        "payments.new_link",
        invoice,
        summary=f"Made a new {taking[provider].label} payment page for {invoice}",
    )
    return _invoice_out(invoice)
