"""The payment providers (0.10.x): Stripe and PayPal, each making the page where
an invoice is paid and reading back what was paid. services.py keeps one page
per invoice per provider that's on, and records the payments."""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from django.utils import timezone

from trammel.core import secrets
from trammel.core.money import format_money as show_money

from . import paypal, stripe
from .errors import BeingPaid
from .models import OnlinePayment, PayLink, PayPalSettings, Provider, StripeSettings

log = logging.getLogger(__name__)

#: Read again on each check, in case a payment landed late in the provider's list.
OVERLAP = dt.timedelta(minutes=10)
#: How far back the first check looks.
FIRST_LOOK = dt.timedelta(days=1)
#: A payment still not through after this long is given up on.
GIVE_UP = dt.timedelta(days=21)


def _parse(stamp: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except TypeError, ValueError:
        return None


def paypal_paid_at(transaction: dict, found: dict) -> dt.datetime:
    """When a PayPal payment was made.

    PayPal's `payment_date_time` can carry the PayPal account's local clock
    time marked "Z" (seen 11/12/2026: "19:30:45Z" for a payment made at 7:30 PM
    Pacific, 03:30:45 UTC, which the invoice's `metadata.last_update_time` gave
    correctly). So the clock time is read both as given and as the shop's local
    time, and the one nearer the invoice's last update (true UTC, normally the
    payment itself) is taken. A date alone is noon that day in the shop's zone.
    """
    zone = timezone.get_current_timezone()
    given = _parse(transaction.get("payment_date_time") or "")
    if given is not None:
        naive = given.replace(tzinfo=None)
        readings = [given if given.tzinfo else naive.replace(tzinfo=dt.UTC)]
        readings.append(naive.replace(tzinfo=zone))
        updated = _parse(
            ((found.get("detail") or {}).get("metadata") or {}).get("last_update_time")
        )
        if updated is not None and updated.tzinfo is not None:
            return min(readings, key=lambda r: abs(r - updated))
        now = timezone.now()
        return next((r for r in reversed(readings) if r <= now), readings[0])
    try:
        day = dt.date.fromisoformat(transaction.get("payment_date") or "")
    except TypeError, ValueError:
        return timezone.now()
    return dt.datetime.combine(day, dt.time(12), tzinfo=zone)


class Base:
    key: str
    label: str
    #: How it's offered in a list of ways to pay: "Card", "PayPal".
    name: str
    settings_model: type

    def settings(self):
        return self.settings_model.load()

    def credentials(self) -> bool:
        raise NotImplementedError

    def active(self) -> bool:
        return self.credentials() and self.settings().enabled

    def test_mode(self) -> bool:
        raise NotImplementedError

    def button(self, amount: Decimal) -> str:
        raise NotImplementedError

    def create(self, invoice, back: str) -> dict:
        """A page for the invoice's balance: {id, url, number}."""
        raise NotImplementedError

    def update(self, link: PayLink, invoice) -> bool:
        """Change an existing page to the invoice's balance; False when the
        provider can't, so a new page is made instead."""
        return False

    def deactivate(self, link_id: str) -> None:
        raise NotImplementedError

    def check(self) -> int:
        """Record what was paid since the last check; how many."""
        raise NotImplementedError


class StripeProvider(Base):
    key, label, name = Provider.STRIPE, "Stripe", "Card"
    settings_model = StripeSettings

    def credentials(self) -> bool:
        return secrets.is_set(stripe.KEY)

    def test_mode(self) -> bool:
        return stripe.test_mode()

    def button(self, amount):
        return f"Pay {show_money(amount)} online"

    def create(self, invoice, back):
        from trammel.core.models import BusinessProfile

        from .services import currency, to_minor

        cur = currency()
        title = f"{invoice.get_kind_display()} {invoice.number}"
        made = stripe.create_link(
            name=title,
            description=f"{BusinessProfile.load().name}: {title}"[:500],
            minor=to_minor(invoice.balance, cur),
            currency=cur,
            metadata={"trammel_invoice": str(invoice.pk), "trammel_number": invoice.number},
            return_url=back,
        )
        return {**made, "number": ""}

    def deactivate(self, link_id):
        stripe.deactivate(link_id)

    def check(self):
        from trammel.billing.models import Invoice

        from .services import record, take

        row = self.settings()
        started = timezone.now()
        since = (row.checked_through or started - FIRST_LOOK) - OVERLAP
        recorded = 0
        for found in stripe.completed_checkouts(since):
            invoice_id = (found.get("metadata") or {}).get("trammel_invoice")
            if not invoice_id or OnlinePayment.objects.filter(session_id=found["id"]).exists():
                continue
            invoice = Invoice.objects.filter(pk=invoice_id).first()
            if invoice is None:
                log.warning(
                    "Stripe checkout %s is for an invoice Trammel doesn't have.", found["id"]
                )
                continue
            currency = (found.get("currency") or "").upper()
            intent = found.get("payment_intent") or ""
            online = take(
                self,
                session_id=found["id"],
                link_id=found.get("payment_link") or "",
                invoice=invoice,
                minor=int(found.get("amount_total") or 0),
                currency=currency,
                reference=intent if isinstance(intent, str) else intent.get("id", ""),
                email=(found.get("customer_details") or {}).get("email") or "",
                paid_at=dt.datetime.fromtimestamp(int(found["created"]), tz=dt.UTC)
                if found.get("created")
                else timezone.now(),
                test_mode=not found.get("livemode", False),
            )
            if found.get("payment_status") in ("paid", "no_payment_required"):
                record(online)
                recorded += 1
        for online in OnlinePayment.objects.filter(
            provider=self.key, status=OnlinePayment.Status.WAITING
        ):
            found = stripe.checkout(online.session_id)
            if found.get("payment_status") in ("paid", "no_payment_required"):
                record(online)
                recorded += 1
            elif timezone.now() - online.paid_at > GIVE_UP:
                OnlinePayment.objects.filter(pk=online.pk).update(
                    status=OnlinePayment.Status.FAILED
                )
        self.settings_model.objects.filter(pk=1).update(checked_through=started)
        return recorded


class PayPalProvider(Base):
    key, label, name = Provider.PAYPAL, "PayPal", "PayPal"
    settings_model = PayPalSettings

    def credentials(self) -> bool:
        return bool(self.settings().client_id) and secrets.is_set(paypal.SECRET_KEY)

    def test_mode(self) -> bool:
        return self.settings().sandbox

    def button(self, amount):
        return f"Pay {show_money(amount)} with PayPal"

    def _fields(self, invoice) -> dict:
        from trammel.core.models import BusinessProfile

        from .services import currency

        title = f"{invoice.get_kind_display()} {invoice.number}"
        return {
            "reference": invoice.number,
            "note": f"{title} from {BusinessProfile.load().name}."[:4000],
            "memo": f"trammel:{invoice.pk}",
            "name": title,
            "value": str(invoice.balance),
            "currency": currency(),
        }

    def create(self, invoice, back):
        # Numbered exactly as in Trammel; PayPal has no way back to the portal.
        return paypal.create_invoice(number=invoice.number, **self._fields(invoice))

    def update(self, link, invoice):
        from .services import currency

        try:
            paypal.update_amount(link.link_id, value=str(invoice.balance), currency=currency())
        except paypal.PayPalError as exc:
            if exc.status in (400, 404, 422):
                log.info("PayPal wouldn't update %s (%s); making a new one.", link.link_id, exc)
                return False
            raise
        return True

    def deactivate(self, link_id):
        if status := paypal.cancel(link_id):
            raise BeingPaid(
                f"PayPal invoice {link_id} is {status.replace('_', ' ').lower()}: it stays "
                "until that payment is recorded, and the invoice isn't offered elsewhere meanwhile."
            )

    def _found(self, found: dict, link: PayLink | None):
        """A paid PayPal invoice, noted once."""
        from trammel.billing.models import Invoice

        from .services import take

        memo = (found.get("detail") or {}).get("memo") or ""
        invoice = link.invoice if link else None
        if invoice is None and memo.startswith("trammel:"):
            invoice = Invoice.objects.filter(pk=memo.split(":", 1)[1] or None).first()
        if invoice is None:
            return None
        payments = found.get("payments") or {}
        paid = payments.get("paid_amount") or found.get("amount") or {}
        transactions = payments.get("transactions") or [{}]
        first = transactions[0] if transactions else {}
        paid_at = paypal_paid_at(first, found)
        recipients = found.get("primary_recipients") or [{}]
        email = ((recipients[0] or {}).get("billing_info") or {}).get("email_address") or ""
        return take(
            self,
            session_id=f"paypal:{found['id']}",
            link_id=found["id"],
            invoice=invoice,
            amount=Decimal(str(paid.get("value") or "0")),
            currency=(paid.get("currency_code") or "").upper(),
            reference=first.get("payment_id") or found["id"],
            email=email,
            paid_at=paid_at,
            test_mode=self.test_mode(),
        )

    def check(self):
        from .services import record

        row = self.settings()
        started = timezone.now()
        # PayPal may give only the date it was paid: look a day either side.
        since = (row.checked_through or started - FIRST_LOOK) - dt.timedelta(days=1)
        recorded = 0
        for summary in paypal.paid_since(since, started + dt.timedelta(days=1)):
            if OnlinePayment.objects.filter(session_id=f"paypal:{summary['id']}").exists():
                continue
            link = PayLink.objects.filter(provider=self.key, link_id=summary["id"]).first()
            found = paypal.invoice(summary["id"])
            online = self._found(found, link)
            if online is not None and found.get("status") in paypal.PAID:
                record(online)
                recorded += 1
        self.settings_model.objects.filter(pk=1).update(checked_through=started)
        return recorded


PROVIDERS: dict[str, Base] = {p.key: p for p in (StripeProvider(), PayPalProvider())}


def get(key: str) -> Base:
    return PROVIDERS[key]
