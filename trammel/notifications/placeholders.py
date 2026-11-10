"""Email template placeholders and preview data, one kind per event."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from trammel.core import placeholders as ph
from trammel.core.placeholders import P

from .events import EVENTS


def _quote_preview():
    from trammel.quotes.placeholders import preview

    return preview()


def _invoice_preview():
    from trammel.billing.placeholders import preview

    return preview()


def _quote_groups():
    from trammel.quotes import placeholders as q

    return (q.QUOTE, q.LINES, q.JOB, ph.CUSTOMER, ph.BUSINESS)


def _invoice_groups():
    from trammel.billing import placeholders as b

    return (b.INVOICE, b.LINES, ph.CUSTOMER, ph.BUSINESS)


def _with(base, **extra):
    def preview():
        ctx, source = base()
        return {**ctx, **extra}, source

    return preview


def _plain(**ctx):
    def preview():
        return {"business": ph.business(), **ctx}, "sample data"

    return preview


def _payment_preview():
    from trammel.billing.models import Payment
    from trammel.core import templating

    payment = Payment.objects.select_related("customer").order_by("-created_at").first()
    if payment is not None:
        return {
            "business": ph.business(),
            "customer": templating.customer_context(payment.customer),
            "payment": {
                "amount": payment.amount,
                "received_on": payment.received_on,
                "method": payment.get_method_display(),
            },
            "credit": Decimal("0"),
        }, f"the payment from {payment.customer}"
    return {
        "business": ph.business(),
        "customer": ph.SAMPLE_CUSTOMER,
        "payment": {
            "amount": Decimal("1529.33"),
            "received_on": timezone.localdate(),
            "method": "Check",
        },
        "credit": Decimal("0"),
    }, "sample data"


PAYMENT = ph.Group(
    "Payment",
    (
        P("payment.amount", "Amount received"),
        P("payment.received_on", "Date received"),
        P("payment.method", "How it was paid"),
        P("credit", "Credit left on the customer's account, if any"),
    ),
)


def _kinds():
    quote_groups, invoice_groups = _quote_groups(), _invoice_groups()
    return {
        "quote_sent": (quote_groups, _quote_preview),
        "quote_approved": (
            (ph.Group("Approval", (P("approved_by", "Who approved it"),)), *quote_groups),
            _with(_quote_preview, approved_by="Grace Hollis"),
        ),
        "quote_declined": (
            (ph.Group("Decline", (P("reason", "The reason given, if any"),)), *quote_groups),
            _with(_quote_preview, reason="Went with a smaller table."),
        ),
        "invoice_issued": (invoice_groups, _invoice_preview),
        "invoice_overdue": (invoice_groups, _invoice_preview),
        "payment_recorded": (
            (PAYMENT, ph.CUSTOMER, ph.BUSINESS),
            _payment_preview,
        ),
        "magic_link": (
            (
                ph.Group(
                    "Sign-in link",
                    (
                        P("link", "The one-time sign-in link"),
                        P("minutes", "Minutes until it expires"),
                    ),
                ),
                ph.BUSINESS,
            ),
            _plain(link=f"{settings.BASE_URL}/accounts/magic/…", minutes=15),
        ),
        "portal_code": (
            (
                ph.Group(
                    "Sign-in code",
                    (P("code", "The one-time code"), P("minutes", "Minutes until it expires")),
                ),
                ph.BUSINESS,
            ),
            _plain(code="482 913", minutes=10),
        ),
        "backup_failed": (
            (
                ph.Group(
                    "Backup",
                    (P("when", "When the backup ran"), P("error", "What went wrong")),
                ),
                ph.BUSINESS,
            ),
            _plain(
                when="11/08/2026 1:00 AM",
                error="pg_dump: error: connection to server failed",
            ),
        ),
        "stock_low": (
            (
                ph.Group(
                    "Low stock",
                    (
                        P(
                            "items",
                            "The materials, each with material, on_hand, restock_at and reorder",
                        ),
                        P("link", "The restock list"),
                    ),
                ),
                ph.BUSINESS,
            ),
            _plain(
                items=[
                    {
                        "material": "Walnut, 4/4",
                        "on_hand": "3.5 board ft",
                        "restock_at": "10 board ft",
                        "reorder": "40 board ft",
                    },
                    {
                        "material": "Danish oil",
                        "on_hand": "0.2 l",
                        "restock_at": "0.5 l",
                        "reorder": "",
                    },
                ],
                link=f"{settings.BASE_URL}/stock/restock/",
            ),
        ),
        "delivery_overdue": (
            (
                ph.Group(
                    "Purchase order",
                    (
                        P("po.number", "Its number"),
                        P("po.vendor", "The vendor"),
                        P("po.ordered_on", "When it was ordered"),
                        P("po.expected_on", "When it was expected"),
                        P("lines", "What is still to arrive"),
                        P("link", "The purchase order"),
                    ),
                ),
                ph.BUSINESS,
            ),
            _plain(
                po={
                    "number": "PO-2026-0042",
                    "vendor": "Hardwood Haven",
                    "ordered_on": timezone.localdate() - dt.timedelta(days=12),
                    "expected_on": timezone.localdate() - dt.timedelta(days=2),
                },
                lines=["40 board ft Walnut, 4/4", "2 l Danish oil"],
                link=f"{settings.BASE_URL}/purchasing/…",
            ),
        ),
        "update_available": (
            (
                ph.Group(
                    "Update",
                    (
                        P("version", "The new version"),
                        P("current", "The version running now"),
                        P("url", "Release notes"),
                    ),
                ),
                ph.BUSINESS,
            ),
            _plain(
                version="1.1.0",
                current=settings.APP_VERSION,
                url="https://github.com/…/releases",
            ),
        ),
    }


def register_all():
    for order, (key, (groups, preview)) in enumerate(_kinds().items()):
        spec = EVENTS[key]
        ph.register(
            ph.Kind(
                key=f"email:{key}",
                title=f"Email: {spec.label}",
                groups=groups,
                preview=preview,
                html=False,
                module="notifications",
                has_subject=True,
                order=100 + order,
                note=f"Goes to: {spec.audience_label}.",
            )
        )


register_all()
