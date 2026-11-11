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
        P(
            "quote.choices_title",
            "A heading for the choices: “Options and alternatives, not in the total”, or "
            "“Not chosen” once approved",
        ),
        P(
            "quote.choices_picked",
            "True while the customer is picking their choices before approving: the lines "
            "and totals are their picks, not the defaults",
        ),
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
        P(
            "line.offer",
            "“included”; “alternate” for the alternative chosen (or suggested) from its "
            "group; “optional” for an option the customer added",
        ),
        P("line.group", "An alternative's group, such as Wood"),
    ),
    loop=("line", "quote.lines"),
)

CHOICES = ph.Group(
    "Each option or alternative left out of the total",
    (
        P("choice.description", "Description"),
        P("choice.quantity", "Quantity"),
        P("choice.unit", "Unit"),
        P("choice.unit_price", "Price per unit"),
        P("choice.total", "Its price"),
        P(
            "choice.difference",
            "What picking it changes in the total: an option's price, or an alternative's "
            "price less its group's choice (negative when it costs less)",
        ),
        P("choice.offer", "“optional” or “alternate”"),
        P("choice.group", "An alternative's group, such as Wood"),
    ),
    loop=("choice", "quote.choices"),
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


APPROVAL = ph.Group(
    "Approval",
    (
        P("approval", "Present once it is approved; use {% if approval %} around the rest"),
        P("approval.name", "Who approved it"),
        P("approval.date", "The day it was approved"),
        P("approval.method", "How it was recorded: by staff, in the portal, or signed in the shop"),
        P("approval.how", "In person, by phone, by email…, when staff recorded it"),
        P(
            "approval.signature",
            "Their drawn signature, as an image address for <img src=…>; empty unless they signed",
        ),
    ),
)


def sample_quote() -> dict:
    lines = [{**line, "offer": "included", "group": ""} for line in ph.sample_lines()]
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
        "choices_title": "Options and alternatives, not in the total",
        "choices_picked": False,
        "choices": [
            {
                "description": "Hard-wax oil finish instead of oil",
                "quantity": "1",
                "unit": "each",
                "unit_price": Decimal("180.00"),
                "discount_pct": Decimal("0"),
                "total": Decimal("180.00"),
                "difference": Decimal("60.00"),
                "offer": "alternate",
                "group": "Finish",
            },
            {
                "description": "Two matching benches",
                "quantity": "2",
                "unit": "each",
                "unit_price": Decimal("450.00"),
                "discount_pct": Decimal("0"),
                "total": Decimal("900.00"),
                "difference": Decimal("900.00"),
                "offer": "optional",
                "group": "",
            },
        ],
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
        "approval": None,
    }, "sample data"


ph.register(
    ph.Kind(
        key="quote",
        title="Quotes",
        groups=(QUOTE, LINES, CHOICES, JOB, ph.CUSTOMER, ph.BUSINESS, AGREEMENT, APPROVAL),
        preview=preview,
        module="quotes",
        permission="quotes.manage_templates",
        order=10,
    )
)
