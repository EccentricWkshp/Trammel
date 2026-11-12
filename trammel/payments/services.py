"""Online payments (0.10.x): for each open invoice, a page to pay its balance at
each provider that's on (a Stripe payment link, a PayPal invoice), kept to the
balance; and payments read back from the providers and recorded. Nothing
calls in: Trammel makes the pages and asks what was paid, about once a minute."""

from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from trammel.billing import services as billing
from trammel.billing.models import OPEN, Invoice, Payment
from trammel.core import modules
from trammel.core.models import BusinessProfile
from trammel.core.money import currency_exponent

from .errors import BeingPaid, ProviderError
from .models import OnlinePayment, PayLink, Provider
from .providers import PROVIDERS, get

log = logging.getLogger(__name__)

METHODS = {Provider.STRIPE: Payment.Method.STRIPE, Provider.PAYPAL: Payment.Method.PAYPAL}


def _online() -> bool:
    from trammel.core.models import SystemSettings

    return modules.is_enabled("payments") and not SystemSettings.load().is_offline


def providers() -> list:
    """The providers taking payments now."""
    if not _online():
        return []
    return [p for p in PROVIDERS.values() if p.active()]


def active() -> bool:
    """Taking payments through any provider."""
    return bool(providers())


def currency() -> str:
    return BusinessProfile.load().currency


_currency = currency


def to_minor(amount: Decimal, cur: str) -> int:
    return int((amount * (10 ** currency_exponent(cur))).to_integral_value())


def from_minor(minor: int, cur: str) -> Decimal:
    return Decimal(minor).scaleb(-currency_exponent(cur))


def return_url(invoice: Invoice) -> str:
    """Where the customer comes back to after paying: the invoice in the portal
    (Trammel's own, or the one on Cloudflare), if they can use it."""
    if not modules.is_enabled("portal"):
        return ""
    from trammel.portal import services as portal

    if not portal.has_access(invoice.customer):
        return ""
    return portal.link(reverse("portal:invoice", args=[invoice.pk])) + "?notice=payment-received"


def provider_for(invoice: Invoice):
    """The one provider the invoice is offered through: the invoice's own choice,
    else its customer's, else the shop's primary, whichever is on first; any
    provider that's on as a last resort. None when none is on."""
    from .models import CustomerChoice, InvoiceChoice, PaymentsSettings

    taking = {p.key: p for p in providers()}
    if not taking:
        return None
    wanted = [
        InvoiceChoice.objects.filter(invoice=invoice).values_list("provider", flat=True).first(),
        CustomerChoice.objects.filter(customer_id=invoice.customer_id)
        .values_list("provider", flat=True)
        .first(),
        PaymentsSettings.load().primary,
    ]
    for key in wanted:
        if key in taking:
            return taking[key]
    return next(iter(taking.values()))


def current_link(invoice: Invoice, provider: str | None = None) -> PayLink | None:
    """The active link for exactly the invoice's balance, at this provider or the
    one the invoice is offered through."""
    if provider is None:
        chosen = provider_for(invoice)
        if chosen is None:
            return None
        provider = chosen.key
    return (
        PayLink.objects.filter(
            invoice=invoice, active=True, amount=invoice.balance, provider=provider
        )
        .order_by("-created_at")
        .first()
    )


def pay_options(invoice: Invoice) -> list[dict]:
    """For billing.pay_links: how the invoice can be paid online now, {label,
    name, url}. One provider only, so it can't be paid twice."""
    chosen = provider_for(invoice)
    link = current_link(invoice, chosen.key) if chosen else None
    if link is None:
        return []
    return [{"label": chosen.button(invoice.balance), "name": chosen.name, "url": link.url}]


def pay_url(invoice: Invoice) -> str:
    options = pay_options(invoice)
    return options[0]["url"] if options else ""


def _payable():
    return Invoice.objects.filter(status__in=OPEN, balance__gt=0).exclude(
        kind=Invoice.Kind.CREDIT_NOTE
    )


def _waiting_invoices() -> set:
    return set(
        OnlinePayment.objects.filter(status=OnlinePayment.Status.WAITING).values_list(
            "invoice_id", flat=True
        )
    )


def _turn_off(link: PayLink) -> None:
    """Close the page; raises BeingPaid (leaving it open) when it's being paid."""
    get(link.provider).deactivate(link.link_id)
    PayLink.objects.filter(pk=link.pk).update(active=False, deactivated_at=timezone.now())


def make_link(invoice: Invoice, provider: str | None = None) -> PayLink:
    """A new page for the invoice's balance at this provider (by default, the
    one it's offered through); every other open page for it is closed first."""
    chosen = get(provider) if provider else provider_for(invoice)
    if chosen is None:
        raise ProviderError("Online payments aren't on.")
    for old in PayLink.objects.filter(invoice=invoice, active=True):
        _turn_off(old)
    back = return_url(invoice)
    made = chosen.create(invoice, back)
    return PayLink.objects.create(
        invoice=invoice,
        provider=chosen.key,
        link_id=made["id"],
        url=made["url"],
        number=made.get("number") or "",
        amount=invoice.balance,
        currency=currency(),
        return_url=back,
        test_mode=chosen.test_mode(),
    )


def keep_links(invoice_ids=None) -> int:
    """Each open invoice with something to pay gets one page, at the one provider
    it's offered through, for exactly its balance and coming back to where the
    customer can now see it. Every other page is closed first, so an invoice is
    never payable in two places; one whose old page is being paid right now
    keeps it and gets no other. One with a payment still being processed gets
    none. Only these invoices, when given. Returns how many pages were made."""
    if not _online():
        return 0
    payable = _payable()
    links = PayLink.objects.filter(active=True)
    if invoice_ids is not None:
        payable = payable.filter(pk__in=invoice_ids)
        links = links.filter(invoice_id__in=invoice_ids)
    waiting = _waiting_invoices()
    wanted = {}
    for invoice in payable.select_related("customer"):
        if invoice.pk not in waiting and (chosen := provider_for(invoice)) is not None:
            wanted[invoice.pk] = (invoice, chosen)
    cur = currency()
    blocked = set()
    # First close what shouldn't be open.
    for link in links.select_related("invoice"):
        provider = get(link.provider)
        target = wanted.get(link.invoice_id)
        invoice = target[0] if target else None
        keep = (
            target is not None
            and target[1].key == link.provider
            and provider.active()
            and link.currency == cur
            and link.return_url == return_url(invoice)
        )
        try:
            if not keep:
                if provider.credentials() or provider.active():
                    _turn_off(link)
                else:
                    # Its credentials are gone: it can't be reached to close.
                    PayLink.objects.filter(pk=link.pk).update(
                        active=False, deactivated_at=timezone.now()
                    )
            elif link.amount != invoice.balance:
                # The same page for the new amount where the provider allows it
                # (a PayPal invoice keeps its number); otherwise a new one.
                if provider.update(link, invoice):
                    PayLink.objects.filter(pk=link.pk).update(amount=invoice.balance)
                else:
                    _turn_off(link)
        except BeingPaid:
            blocked.add(link.invoice_id)
    # Then make what's missing.
    have = set(PayLink.objects.filter(active=True).values_list("invoice_id", flat=True))
    made = 0
    for invoice_id, (invoice, chosen) in wanted.items():
        if invoice_id not in have and invoice_id not in blocked:
            make_link(invoice, chosen.key)
            made += 1
    return made


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


def take(
    provider,
    *,
    session_id: str,
    link_id: str,
    invoice: Invoice,
    currency: str,
    reference: str,
    email: str,
    paid_at,
    test_mode: bool,
    minor: int | None = None,
    amount: Decimal | None = None,
) -> OnlinePayment:
    """A payment the provider reports, noted (waiting) and its page marked used."""
    cur = currency or _currency()
    link = PayLink.objects.filter(provider=provider.key, link_id=link_id).first()
    if link is not None and link.active:
        # Paid once: the provider won't take it again (Stripe turns it off itself).
        PayLink.objects.filter(pk=link.pk).update(active=False, deactivated_at=timezone.now())
    return OnlinePayment.objects.create(
        provider=provider.key,
        session_id=session_id,
        link=link,
        invoice=invoice,
        amount=amount if amount is not None else from_minor(minor or 0, cur),
        currency=cur,
        reference=(reference or "")[:100],
        email=(email or "")[:254],
        paid_at=paid_at,
        test_mode=test_mode,
    )


def _check_all() -> tuple[int, list]:
    """Every active provider's payments since its last check: (how many were
    recorded, the errors). One provider failing doesn't stop the others."""
    recorded, problems = 0, []
    for provider in providers():
        try:
            recorded += provider.check()
        except ProviderError as exc:
            _problem(provider, exc)
            problems.append(exc)
        else:
            provider.settings_model.objects.filter(pk=1).update(
                last_check_at=timezone.now(), last_error=""
            )
    return recorded, problems


def check_payments() -> int:
    """Every active provider's payments; raises the first error, after the
    others have been checked."""
    recorded, problems = _check_all()
    if problems:
        raise problems[0]
    return recorded


def _problem(provider, exc) -> None:
    provider.settings_model.objects.filter(pk=1).update(
        last_check_at=timezone.now(), last_error=str(exc)[:2000], last_error_at=timezone.now()
    )
    log.warning("Online payments check (%s) failed: %s", provider.label, exc)


@transaction.atomic
def record(online: OnlinePayment) -> Payment:
    """Record a payment made online: against the invoice as far as it's still
    owed, the rest held as the customer's credit (it was voided, or paid some
    other way, meanwhile)."""
    online = OnlinePayment.objects.select_for_update().get(pk=online.pk)
    if online.payment_id:
        return online.payment
    invoice = Invoice.objects.get(pk=online.invoice_id)
    share = min(online.amount, invoice.balance) if invoice.status in OPEN else Decimal("0")
    share = max(share, Decimal("0"))
    label = online.get_provider_display()
    what = "checkout" if online.provider == Provider.STRIPE else "invoice"
    payment = billing.record_payment(
        invoice.customer,
        amount=online.amount,
        received_on=timezone.localdate(online.paid_at),
        method=METHODS[online.provider],
        reference=online.reference or online.session_id[:100],
        notes=f"Paid online through {label} ({what} {online.session_id})"[:300],
        allocations={invoice: share} if share > 0 else {},
    )
    OnlinePayment.objects.filter(pk=online.pk).update(
        status=OnlinePayment.Status.RECORDED, payment=payment
    )
    credit = online.amount - share
    transaction.on_commit(lambda: _tell_staff(online, invoice, credit))
    return payment


def _tell_staff(online: OnlinePayment, invoice: Invoice, credit: Decimal) -> None:
    if not modules.is_enabled("notifications"):
        return
    from trammel.core import templating
    from trammel.notifications.services import notify

    notify(
        "paid_online",
        entity=invoice,
        context={
            **billing.document_context(invoice),
            "customer": templating.customer_context(invoice.customer),
            "amount": online.amount,
            "provider": online.get_provider_display(),
            "reference": online.reference or online.session_id,
            "email": online.email,
            "credit": credit,
            "link": settings.BASE_URL + invoice.get_absolute_url(),
        },
    )


def run_check() -> dict:
    """The periodic check: record what was paid, then keep the pages current.
    Problems are kept for the settings page rather than raised."""
    if not active():
        return {}
    recorded, problems = _check_all()
    errors = [str(exc) for exc in problems]
    made = 0
    try:
        made = keep_links()
    except ProviderError as exc:
        errors.append(str(exc))
        for provider in providers():
            if isinstance(exc, _error_type(provider)):
                _problem(provider, exc)
    if errors:
        return {"error": " ".join(errors), "recorded": recorded, "links_made": made}
    return {"recorded": recorded, "links_made": made}


def _error_type(provider):
    from . import paypal, stripe

    return stripe.StripeError if provider.key == Provider.STRIPE else paypal.PayPalError


def note_problem(exc: ProviderError) -> None:
    """Keep an error from outside the periodic check on its provider's settings."""
    for provider in PROVIDERS.values():
        if isinstance(exc, _error_type(provider)):
            _problem(provider, exc)


def _follow(invoice_ids) -> None:
    from trammel.core import tasks

    ids = [str(i) for i in invoice_ids]
    if ids:
        transaction.on_commit(lambda: tasks.enqueue("payments.keep_links", {"invoices": ids}))


def choose_for_customer(customer, provider: str | None) -> None:
    """Offer this customer's invoices through `provider` (None: the primary)."""
    from trammel.core import audit

    from .models import CustomerChoice

    if provider:
        get(provider)  # a known provider
        CustomerChoice.objects.update_or_create(customer=customer, defaults={"provider": provider})
    else:
        CustomerChoice.objects.filter(customer=customer).delete()
    label = get(provider).label if provider else "the shop's primary provider"
    audit.record(
        "payments.customer_choice",
        customer,
        summary=f"Offered {customer}'s invoices online through {label}",
    )
    _follow(_payable().filter(customer=customer).values_list("pk", flat=True))


def choose_for_invoice(invoice: Invoice, provider: str | None) -> None:
    """Offer this invoice through `provider` (None: as its customer is)."""
    from trammel.core import audit

    from .models import InvoiceChoice

    if provider:
        get(provider)
        InvoiceChoice.objects.update_or_create(invoice=invoice, defaults={"provider": provider})
    else:
        InvoiceChoice.objects.filter(invoice=invoice).delete()
    label = get(provider).label if provider else "its customer's provider"
    audit.record("payments.invoice_choice", invoice, summary=f"Offered {invoice} through {label}")
    _follow([invoice.pk])


def choice_of(invoice: Invoice) -> dict:
    """How the invoice is offered and why: {provider, why, chosen (its own)}."""
    from .models import CustomerChoice, InvoiceChoice, PaymentsSettings

    chosen = provider_for(invoice)
    own = InvoiceChoice.objects.filter(invoice=invoice).values_list("provider", flat=True).first()
    customer = (
        CustomerChoice.objects.filter(customer_id=invoice.customer_id)
        .values_list("provider", flat=True)
        .first()
    )
    if chosen is None:
        why = ""
    elif own == chosen.key:
        why = "chosen for this invoice"
    elif customer == chosen.key:
        why = "the customer's choice"
    elif PaymentsSettings.load().primary == chosen.key:
        why = "the shop's primary"
    else:
        why = "the only one on"
    return {"provider": chosen, "why": why, "chosen": own or "", "customer": customer or ""}
