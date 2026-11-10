"""Quote template placeholders and preview data."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.utils import timezone

from trammel.core import placeholders as ph
from trammel.core.placeholders import P

QUOTE = ph.Group(
    "Quote",
    (
        P("quote.document_title", "“Quote” or “Change order”"),
        P("quote.is_change_order", "True for a change order"),
        P("quote.number", "Quote number"),
        P("quote.title", "Title"),
        P("quote.revision", "Revision number"),
        P("quote.status", "Status, e.g. Sent"),
        P("quote.date", "Date sent (or created)"),
        P("quote.valid_until", "Valid until"),
        P("quote.description", "What is being done, in narrative terms"),
        P("quote.notes", "Notes for the customer"),
        P("quote.lines_total", "Total of the lines"),
        P("quote.discount_pct", "Quote discount (%)"),
        P("quote.discount", "Quote discount amount"),
        P("quote.minimum_adjustment", "Added to reach the minimum job charge"),
        P("quote.subtotal", "Subtotal"),
        P("quote.shipping", "Shipping or delivery charge"),
        P("quote.shipping_label", "“Shipping” or “Delivery”"),
        P("quote.fulfillment", "Pickup, delivery or shipping"),
        P("quote.tax_label", "Tax line label, e.g. Sales tax (10.5%)"),
        P("quote.tax", "Estimated sales tax"),
        P("quote.total", "Total"),
        P("quote.deposit", "Deposit due on approval"),
        P("quote.deposit_percent", "Deposit as a percentage, if set that way"),
    ),
)

LINES = ph.Group(
    "Each line",
    (
        P("line.description", "Description"),
        P("line.quantity", "Quantity"),
        P("line.unit", "Unit, e.g. hour, board ft"),
        P("line.unit_price", "Price per unit"),
        P("line.discount_pct", "Line discount (%)"),
        P("line.total", "Line total"),
    ),
    loop=("line", "quote.lines"),
)

JOB = ph.Group("Job", (P("job.number", "Job number"), P("job.title", "Job title")))

AGREEMENT = ph.Group(
    "Customer agreement",
    (
        P(
            "agreement",
            "The agreement text for this job, if one was issued; on a change order, "
            "its amended terms",
        ),
    ),
)


def sample_quote() -> dict:
    lines = ph.sample_lines()
    subtotal = sum((line["total"] for line in lines), Decimal("0"))
    shipping = Decimal("85.00")
    tax = Decimal("290.66")
    return {
        "document_title": "Quote",
        "is_change_order": False,
        "number": "Q-2026-0012",
        "title": "Walnut dining table",
        "revision": 2,
        "status": "Sent",
        "date": timezone.localdate(),
        "valid_until": timezone.localdate() + dt.timedelta(days=30),
        "description": "A solid walnut dining table seating six, with a breadboard top, "
        "tapered legs and an oil finish. The top is glued up from book-matched boards.",
        "notes": "Price includes delivery within 25 miles.",
        "lines": lines,
        "lines_total": subtotal,
        "discount_pct": Decimal("0"),
        "discount": Decimal("0"),
        "minimum_adjustment": Decimal("0"),
        "subtotal": subtotal,
        "shipping": shipping,
        "shipping_label": "Delivery",
        "fulfillment": "Delivery",
        "tax_label": "Sales tax (10.5%)",
        "tax": tax,
        "total": subtotal + shipping + tax,
        "deposit": Decimal("1529.33"),
        "deposit_percent": Decimal("50"),
    }


def preview() -> tuple[dict, str]:
    from .models import QuoteRevision
    from .services import document_context

    revision = (
        QuoteRevision.objects.select_related("quote__job__customer").order_by("-created_at").first()
    )
    if revision is not None:
        return document_context(revision), (
            f"quote {revision.quote.number}, revision {revision.number}"
        )
    return {
        "business": ph.business(),
        "customer": ph.SAMPLE_CUSTOMER,
        "job": {"number": ph.SAMPLE_JOB["number"], "title": ph.SAMPLE_JOB["title"]},
        "quote": sample_quote(),
        "agreement": "",
    }, "sample data"


ph.register(
    ph.Kind(
        key="quote",
        title="Quotes",
        groups=(QUOTE, LINES, JOB, ph.CUSTOMER, ph.BUSINESS, AGREEMENT),
        preview=preview,
        module="quotes",
        permission="quotes.manage_templates",
        order=10,
    )
)
