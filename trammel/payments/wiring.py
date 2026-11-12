"""Online payments' place in billing: the link where an invoice can be paid,
kept to the invoice's balance, and a panel on the invoice for staff."""

from __future__ import annotations

from django.db import transaction

from trammel.billing import services as billing
from trammel.billing.models import OPEN
from trammel.core import access, panels, tasks

from . import services


def _follow(invoice) -> None:
    """An invoice was issued, voided or its balance changed: its link follows,
    in the background, once this is saved."""
    if not services.active():
        return
    invoice_id = str(invoice.pk)
    transaction.on_commit(
        lambda: tasks.enqueue("payments.keep_links", {"invoice": invoice_id}, dedupe=True)
    )


billing.pay_links.append(services.pay_options)
billing.on_issued.append(_follow)
billing.on_balance_changed.append(_follow)


@panels.register("billing.invoice")
def online_payment(request, invoice):
    """The invoice's payment link and what was paid through it."""
    from trammel.core import modules

    from .models import OnlinePayment

    if not modules.is_enabled("payments") or invoice.is_draft or invoice.is_credit_note:
        return None
    if not access.has_perm(request.actor, "billing.view", invoice):
        return None
    received = list(OnlinePayment.objects.filter(invoice=invoice).select_related("payment"))
    on = services.active()
    if not on and not received:
        return None
    return panels.Panel(
        "Online payment",
        "payments/_invoice_panel.html",
        {
            "invoice": invoice,
            "on": on,
            "choice": services.choice_of(invoice) if on else None,
            "link": services.current_link(invoice) if on else None,
            "providers": services.providers(),
            "received": received,
            "waiting": any(r.status == OnlinePayment.Status.WAITING for r in received),
            "can_manage": access.has_perm(request.actor, "payments.manage"),
            "payable": invoice.status in OPEN and invoice.balance > 0,
        },
        order=20,
    )


@panels.register("customers.customer")
def customer_payments(request, customer):
    """Which provider the customer's invoices are offered through."""
    from .models import CustomerChoice, PaymentsSettings

    if not services.active() or customer.is_internal or customer.is_placeholder:
        return None
    if not access.has_perm(request.actor, "payments.manage"):
        return None
    taking = services.providers()
    if len(taking) < 2:
        return None  # nothing to choose between
    return panels.Panel(
        "Online payments",
        "payments/_customer_panel.html",
        {
            "customer": customer,
            "providers": taking,
            "chosen": CustomerChoice.objects.filter(customer=customer)
            .values_list("provider", flat=True)
            .first()
            or "",
            "primary": PaymentsSettings.load().get_primary_display(),
        },
        order=92,
    )
