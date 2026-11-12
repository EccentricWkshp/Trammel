"""Online payments (0.10.0): a payment link for each open invoice, kept to its
balance, and payments read back from Stripe and recorded. Nothing calls in:
Trammel makes the links and asks Stripe what was paid, about once a minute."""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from trammel.billing import services as billing
from trammel.billing.models import OPEN, Invoice, Payment
from trammel.core import modules, secrets
from trammel.core.models import BusinessProfile
from trammel.core.money import currency_exponent

from . import stripe
from .models import OnlinePayment, PayLink, Provider, StripeSettings

log = logging.getLogger(__name__)

#: Read again on each check, in case a checkout landed late in Stripe's list.
OVERLAP = dt.timedelta(minutes=10)
#: How far back the first check looks.
FIRST_LOOK = dt.timedelta(days=1)
#: A bank payment still not through after this long is given up on.
GIVE_UP = dt.timedelta(days=21)


def active() -> bool:
    """Taking payments: the module on, Stripe switched on, a key, and outbound
    traffic allowed."""
    from trammel.core.models import SystemSettings

    return (
        modules.is_enabled("payments")
        and StripeSettings.load().enabled
        and secrets.is_set(stripe.KEY)
        and not SystemSettings.load().is_offline
    )


def _currency() -> str:
    return BusinessProfile.load().currency


def to_minor(amount: Decimal, currency: str) -> int:
    return int((amount * (10 ** currency_exponent(currency))).to_integral_value())


def from_minor(minor: int, currency: str) -> Decimal:
    return Decimal(minor).scaleb(-currency_exponent(currency))


def return_url(invoice: Invoice) -> str:
    """Where the customer comes back to after paying: the invoice in the portal
    (Trammel's own, or the one on Cloudflare), if they can use it."""
    if not modules.is_enabled("portal"):
        return ""
    from trammel.portal import services as portal

    if not portal.has_access(invoice.customer):
        return ""
    return portal.link(reverse("portal:invoice", args=[invoice.pk])) + "?notice=payment-received"


def current_link(invoice: Invoice) -> PayLink | None:
    """The active link for exactly the invoice's balance, if there is one."""
    return (
        PayLink.objects.filter(invoice=invoice, active=True, amount=invoice.balance)
        .order_by("-created_at")
        .first()
    )


def pay_url(invoice: Invoice) -> str:
    """For billing.pay_url: the current link's address, while taking payments."""
    if not active():
        return ""
    link = current_link(invoice)
    return link.url if link else ""


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
    stripe.deactivate(link.link_id)
    PayLink.objects.filter(pk=link.pk).update(active=False, deactivated_at=timezone.now())


def make_link(invoice: Invoice) -> PayLink:
    """A new link for the invoice's balance; any active one is turned off."""
    for old in PayLink.objects.filter(invoice=invoice, active=True):
        _turn_off(old)
    currency = _currency()
    business = BusinessProfile.load().name
    title = f"{invoice.get_kind_display()} {invoice.number}"
    back = return_url(invoice)
    made = stripe.create_link(
        name=title,
        description=f"{business}: {title}"[:500],
        minor=to_minor(invoice.balance, currency),
        currency=currency,
        metadata={"trammel_invoice": str(invoice.pk), "trammel_number": invoice.number},
        return_url=back,
    )
    return PayLink.objects.create(
        invoice=invoice,
        provider=Provider.STRIPE,
        link_id=made["id"],
        url=made["url"],
        amount=invoice.balance,
        currency=currency,
        return_url=back,
        test_mode=stripe.test_mode(),
    )


def keep_links(invoice_ids=None) -> int:
    """Each open invoice with something to pay gets one active link, for exactly
    its balance and coming back to where the customer can now see it; links for
    anything else are turned off. One with a payment still being processed gets
    none, so it can't be paid twice. Only these invoices, when given. Returns
    how many links were made."""
    if not active():
        return 0
    payable = _payable()
    links = PayLink.objects.filter(active=True)
    if invoice_ids is not None:
        payable = payable.filter(pk__in=invoice_ids)
        links = links.filter(invoice_id__in=invoice_ids)
    waiting = _waiting_invoices()
    wanted = {}
    for invoice in payable.select_related("customer"):
        if invoice.pk not in waiting:
            wanted[invoice.pk] = invoice
    made = 0
    for link in links.select_related("invoice"):
        invoice = wanted.get(link.invoice_id)
        stale = (
            invoice is None
            or link.amount != invoice.balance
            or link.currency != _currency()
            or link.return_url != return_url(invoice)
        )
        if stale:
            _turn_off(link)
    have = set(PayLink.objects.filter(active=True).values_list("invoice_id", flat=True))
    for invoice_id, invoice in wanted.items():
        if invoice_id not in have:
            make_link(invoice)
            made += 1
    return made


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


def check_payments() -> int:
    """Read completed checkouts from Stripe since the last check and record the
    ones paid; bank payments still processing are looked at again each time.
    Returns how many were recorded."""
    row = StripeSettings.load()
    started = timezone.now()
    since = (row.checked_through or started - FIRST_LOOK) - OVERLAP
    recorded = 0
    for found in stripe.completed_checkouts(since):
        invoice_id = (found.get("metadata") or {}).get("trammel_invoice")
        if not invoice_id or OnlinePayment.objects.filter(session_id=found["id"]).exists():
            continue
        invoice = Invoice.objects.filter(pk=invoice_id).first()
        if invoice is None:
            log.warning("Stripe checkout %s is for an invoice Trammel doesn't have.", found["id"])
            continue
        online = _take(found, invoice)
        if found.get("payment_status") in ("paid", "no_payment_required"):
            record(online)
            recorded += 1
    for online in OnlinePayment.objects.filter(status=OnlinePayment.Status.WAITING):
        found = stripe.checkout(online.session_id)
        if found.get("payment_status") in ("paid", "no_payment_required"):
            record(online)
            recorded += 1
        elif timezone.now() - online.paid_at > GIVE_UP:
            OnlinePayment.objects.filter(pk=online.pk).update(status=OnlinePayment.Status.FAILED)
    StripeSettings.objects.filter(pk=1).update(checked_through=started)
    return recorded


def _take(found: dict, invoice: Invoice) -> OnlinePayment:
    """A completed checkout, noted (waiting) and its link marked used."""
    currency = (found.get("currency") or _currency()).upper()
    link = PayLink.objects.filter(link_id=found.get("payment_link") or "").first()
    if link is not None and link.active:
        # Stripe turns a single-use link off itself once it's paid.
        PayLink.objects.filter(pk=link.pk).update(active=False, deactivated_at=timezone.now())
    intent = found.get("payment_intent") or ""
    return OnlinePayment.objects.create(
        provider=Provider.STRIPE,
        session_id=found["id"],
        link=link,
        invoice=invoice,
        amount=from_minor(int(found.get("amount_total") or 0), currency),
        currency=currency,
        reference=(intent if isinstance(intent, str) else intent.get("id", ""))[:100],
        email=((found.get("customer_details") or {}).get("email") or "")[:254],
        paid_at=dt.datetime.fromtimestamp(int(found.get("created") or 0), tz=dt.UTC)
        if found.get("created")
        else timezone.now(),
        test_mode=not found.get("livemode", False),
    )


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
    payment = billing.record_payment(
        invoice.customer,
        amount=online.amount,
        received_on=timezone.localdate(online.paid_at),
        method=Payment.Method.STRIPE,
        reference=online.reference or online.session_id[:100],
        notes=f"Paid online through Stripe (checkout {online.session_id})"[:300],
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
    """The periodic check: record what was paid, then keep the links current.
    Problems are kept for the settings page rather than raised."""
    if not active():
        return {}
    try:
        recorded = check_payments()
        made = keep_links()
    except stripe.StripeError as exc:
        StripeSettings.objects.filter(pk=1).update(
            last_check_at=timezone.now(), last_error=str(exc)[:2000], last_error_at=timezone.now()
        )
        log.warning("Online payments check failed: %s", exc)
        return {"error": str(exc)}
    StripeSettings.objects.filter(pk=1).update(last_check_at=timezone.now(), last_error="")
    return {"recorded": recorded, "links_made": made}
