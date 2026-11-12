"""Invoice template placeholders and preview data."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.utils import timezone

from trammel.core import placeholders as ph
from trammel.core.placeholders import P

INVOICE = ph.Group(
    "Invoice",
    (
        P("invoice.number", "Invoice number (DRAFT until issued)"),
        P("invoice.kind", "Deposit, Progress, Final, Sale or Credit note"),
        P("invoice.is_credit_note", "True for a credit note"),
        P("invoice.credit_for", "For a credit note: the invoice it credits"),
        P("invoice.status", "Status, e.g. Issued, Paid"),
        P("invoice.date", "Invoice date"),
        P("invoice.due_date", "Due date"),
        P("invoice.terms_days", "Payment terms in days"),
        P("invoice.job", "Job number"),
        P("invoice.job_title", "Job title"),
        P("invoice.ship_to", "Ship-to address, as a list of lines"),
        P("invoice.subtotal", "Subtotal"),
        P("invoice.shipping", "Shipping or delivery charge"),
        P("invoice.shipping_label", "“Shipping” or “Delivery”"),
        P("invoice.fulfillment", "Pickup, delivery or shipping"),
        P("invoice.tax_label", "Tax line label, e.g. Sales tax (10.5%)"),
        P("invoice.tax_rate", "Combined tax rate (%)"),
        P("invoice.tax_total", "Sales tax"),
        P("invoice.exemption_number", "Customer's exemption number, if exempt"),
        P("invoice.total", "Total"),
        P("invoice.paid", "Paid and credited so far"),
        P("invoice.written_off", "Written off"),
        P("invoice.balance", "Balance due"),
        P("invoice.pay_url", "Where the customer can pay it online, when online payments are on"),
        P("invoice.notes", "Notes for the customer"),
    ),
)

LINES = ph.Group(
    "Each line",
    (
        P("line.description", "Description"),
        P("line.quantity", "Quantity"),
        P("line.unit", "Unit"),
        P("line.unit_price", "Price per unit"),
        P("line.discount_pct", "Line discount (%)"),
        P("line.total", "Line total"),
        P("line.taxable", "Whether sales tax applies (true or false)"),
    ),
    loop=("line", "invoice.lines"),
)

TAXES = ph.Group(
    "Each tax (for a detailed breakdown)",
    (
        P("tax.name", "Jurisdiction, e.g. the state or city"),
        P("tax.code", "Its code"),
        P("tax.rate", "Rate (%)"),
        P("tax.amount", "Amount"),
    ),
    loop=("tax", "invoice.taxes"),
)


def sample_invoice() -> dict:
    lines = ph.sample_lines()
    subtotal = sum((line["total"] for line in lines), Decimal("0"))
    shipping, tax = Decimal("85.00"), Decimal("290.66")
    total = subtotal + shipping + tax
    today = timezone.localdate()
    return {
        "number": "INV-2026-0031",
        "kind": "Final invoice",
        "is_credit_note": False,
        "credit_for": "",
        "status": "Issued",
        "date": today,
        "due_date": today + dt.timedelta(days=30),
        "terms_days": 30,
        "job": ph.SAMPLE_JOB["number"],
        "job_title": ph.SAMPLE_JOB["title"],
        "ship_to": ph.SAMPLE_CUSTOMER["address_lines"],
        "lines": lines,
        "subtotal": subtotal,
        "shipping": shipping,
        "shipping_label": "Delivery",
        "fulfillment": "Delivery",
        "tax_label": "Sales tax (10.5%)",
        "tax_rate": Decimal("10.5"),
        "taxes": [
            {
                "name": "Washington",
                "code": "53",
                "rate": Decimal("6.5"),
                "amount": Decimal("177.06"),
            },
            {"name": "Tacoma", "code": "2717", "rate": Decimal("4"), "amount": Decimal("113.60")},
        ],
        "tax_total": tax,
        "exemption_number": "",
        "total": total,
        "paid": Decimal("1529.33"),
        "written_off": Decimal("0"),
        "balance": total - Decimal("1529.33"),
        "pay_url": "https://buy.stripe.com/…",
        "notes": "Thank you for your business.",
    }


def preview() -> tuple[dict, str]:
    from .models import Invoice
    from .services import document_context

    invoice = (
        Invoice.objects.filter(number__gt="")
        .select_related("customer", "job")
        .order_by("-created_at")
        .first()
        or Invoice.objects.select_related("customer", "job").order_by("-created_at").first()
    )
    if invoice is not None:
        return document_context(invoice), f"invoice {invoice.number or '(draft)'}"
    return {
        "business": ph.business(),
        "customer": ph.SAMPLE_CUSTOMER,
        "invoice": sample_invoice(),
    }, "sample data"


ph.register(
    ph.Kind(
        key="invoice",
        title="Invoices",
        groups=(INVOICE, LINES, TAXES, ph.CUSTOMER, ph.BUSINESS),
        preview=preview,
        module="billing",
        permission="billing.manage_templates",
        order=20,
    )
)
