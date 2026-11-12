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

from . import services, stripe
from .models import OnlinePayment, PayLink, StripeSettings

router = Router(tags=["online payments"])

PERMISSION = "payments.manage"


class PaymentsStatusOut(Schema):
    #: Switched on in the settings.
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


class PaymentsSettingsIn(Schema):
    enabled: bool | None = None
    #: Write-only: a restricted key (rk_…) or secret key (sk_…).
    stripe_key: str | None = None


class CheckOut(Schema):
    recorded: int = 0
    links_made: int = 0
    error: str = ""


class LinkOut(Schema):
    url: str
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
    #: The current payment link, for exactly the balance; null when there's none.
    link: LinkOut | None
    payments: list[OnlinePaymentOut]


def _status() -> dict:
    from .views import status

    found = status()
    row = found["row"]
    return {
        "enabled": row.enabled,
        "active": found["active"],
        "key_set": found["key_set"],
        "test_mode": found["test_mode"],
        "links": found["links"],
        "last_check_at": row.last_check_at,
        "last_error": row.last_error,
        "last_error_at": row.last_error_at,
    }


@router.get("/settings", response=PaymentsStatusOut)
def get_settings(request):
    access.require(request.actor, PERMISSION)
    return _status()


@router.patch("/settings", response=PaymentsStatusOut)
def update_settings(request, payload: PaymentsSettingsIn):
    """Checked as on the settings page; the key is never returned."""
    from .views import StripeForm

    access.require(request.actor, PERMISSION)
    row = StripeSettings.load()
    data = {"enabled": row.enabled, "stripe_key": ""}
    data.update(payload.dict(exclude_unset=True, exclude_none=True))
    if not data["enabled"]:
        data.pop("enabled")  # an unchecked box, as the form expects
    form = StripeForm(data, instance=row)
    if not form.is_valid():
        raise ValidationError({f: list(e) for f, e in form.errors.items()})
    form.save()
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


def _invoice_out(invoice: Invoice) -> dict:
    link: PayLink | None = services.current_link(invoice) if services.active() else None
    return {
        "invoice": invoice.pk,
        "balance": invoice.balance,
        "link": (
            {
                "url": link.url,
                "amount": link.amount,
                "currency": link.currency,
                "test_mode": link.test_mode,
                "created_at": link.created_at,
            }
            if link
            else None
        ),
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


@router.post("/invoices/{invoice_id}/link", response=InvoicePaymentsOut)
def new_link(request, invoice_id: UUID):
    """A fresh payment link for the invoice's balance; the old one stops working."""
    invoice = _invoice(request, invoice_id)
    if not services.active():
        raise ValidationError("Online payments aren't on.")
    if not (invoice.status in OPEN and invoice.balance > 0 and not invoice.is_credit_note):
        raise ValidationError("Only an open invoice with a balance can be paid online.")
    try:
        services.make_link(invoice)
    except stripe.StripeError as exc:
        raise ValidationError(str(exc)) from None
    audit.record("payments.new_link", invoice, summary=f"Made a new payment link for {invoice}")
    return _invoice_out(invoice)
