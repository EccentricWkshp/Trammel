"""The notification events (FR-NTF-2) and their default templates."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    key: str
    label: str
    audience: str  # "customer", "staff", or "user" (the person it is about)
    subject: str
    body: str
    #: The module whose records it is about; it is listed only while that is on.
    module: str = ""

    @property
    def audience_short(self) -> str:
        return {"customer": "Customer", "staff": "Staff", "user": "Person signing in"}[
            self.audience
        ]

    @property
    def audience_label(self) -> str:
        return {
            "customer": "the customer",
            "staff": "staff (the recipients chosen above)",
            "user": "the person signing in",
        }[self.audience]


EVENTS: dict[str, Event] = {
    e.key: e
    for e in [
        Event(
            "quote_sent",
            "Quote or change order sent to the customer",
            "customer",
            "{{ quote.document_title }} {{ quote.number }} from {{ business.name }}",
            "Hello {{ customer.name }},\n\nPlease find {{ quote.document_title|lower }} "
            "{{ quote.number }} for "
            '"{{ quote.title }}" attached. The total is {{ quote.total|money }}'
            "{% if quote.valid_until %}, valid until {{ quote.valid_until|date }}{% endif %}.\n\n"
            "Reply to this email with any questions.\n\n{{ business.name }}\n{{ business.phone }}\n",
        ),
        Event(
            "quote_approved",
            "Quote or change order approved",
            "staff",
            "Approved: {{ quote.number }} ({{ customer.name }})",
            "{{ approved_by }} approved {{ quote.document_title|lower }} {{ quote.number }} "
            "revision {{ quote.revision }} for "
            "{{ customer.name }}: {{ quote.total|money }}.\n",
        ),
        Event(
            "quote_declined",
            "Quote or change order declined",
            "staff",
            "Declined: {{ quote.number }} ({{ customer.name }})",
            "{{ quote.document_title }} {{ quote.number }} for {{ customer.name }} was declined."
            "{% if reason %}\nReason: {{ reason }}{% endif %}\n",
        ),
        Event(
            "invoice_issued",
            "Invoice sent to the customer",
            "customer",
            "{{ invoice.kind }} {{ invoice.number }} from {{ business.name }}",
            "Hello {{ customer.name }},\n\nPlease find {{ invoice.kind|lower }} {{ invoice.number }}"
            " attached for {{ invoice.total|money }}"
            "{% if invoice.due_date %}, due {{ invoice.due_date|date }}{% endif %}.\n\n"
            "Thank you,\n{{ business.name }}\n",
        ),
        Event(
            "payment_recorded",
            "Payment receipt to the customer",
            "customer",
            "Payment received — thank you",
            "Hello {{ customer.name }},\n\nWe received your payment of {{ payment.amount|money }} "
            "on {{ payment.received_on|date }}.{% if credit %} {{ credit|money }} is held as credit "
            "on your account.{% endif %}\n\nThank you,\n{{ business.name }}\n",
        ),
        Event(
            "invoice_overdue",
            "Overdue invoice reminder",
            "customer",
            "Reminder: invoice {{ invoice.number }} is overdue",
            "Hello {{ customer.name }},\n\nInvoice {{ invoice.number }} was due on "
            "{{ invoice.due_date|date }}. The balance is {{ invoice.balance|money }}.\n\n"
            "If you have already paid, thank you, and please disregard this reminder.\n\n"
            "{{ business.name }}\n",
        ),
        Event(
            "magic_link",
            "Sign-in link for staff",
            "user",
            "Your sign-in link for {{ business.name }}",
            "Use this link to sign in. It works once and expires in {{ minutes }} minutes.\n\n"
            "{{ link }}\n\nIf you did not ask for it, ignore this email.\n",
        ),
        Event(
            "portal_code",
            "Customer portal sign-in code",
            "customer",
            "Your sign-in code for {{ business.name }}",
            "Your code is {{ code }}. It expires in {{ minutes }} minutes.\n",
        ),
        Event(
            "backup_failed",
            "Backup failed",
            "staff",
            "Trammel backup failed",
            "The backup on {{ when }} failed:\n\n{{ error }}\n",
        ),
        Event(
            "update_available",
            "Update available",
            "staff",
            "Trammel {{ version }} is available",
            "Trammel {{ version }} has been released; this instance runs {{ current }}.\n"
            "{{ url }}\n",
        ),
        Event(
            "stock_low",
            "Stock at its restock level",
            "staff",
            "Low stock: {{ items|length }} material{% if items|length != 1 %}s{% endif %}",
            "These materials have reached their restock level:\n\n"
            "{% for item in items %}- {{ item.material }}: {{ item.on_hand }} left "
            "(restock at {{ item.restock_at }}"
            "{% if item.reorder %}, reorder {{ item.reorder }}{% endif %})\n{% endfor %}\n"
            "Nothing has been ordered. The restock list: {{ link }}\n",
            module="stock",
        ),
        Event(
            "delivery_overdue",
            "Delivery past its expected date",
            "staff",
            "Late delivery: {{ po.number }} from {{ po.vendor }}",
            "Purchase order {{ po.number }} from {{ po.vendor }} was expected on "
            "{{ po.expected_on|date }}{% if po.ordered_on %} (ordered "
            "{{ po.ordered_on|date }}){% endif %}. Still to arrive:\n\n"
            "{% for line in lines %}- {{ line }}\n{% endfor %}\n{{ link }}\n",
            module="purchasing",
        ),
    ]
}


def listed() -> list[Event]:
    """The events whose module is on."""
    from trammel.core import modules

    return [e for e in EVENTS.values() if not e.module or modules.is_enabled(e.module)]
