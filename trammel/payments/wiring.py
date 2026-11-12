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


billing.pay_links.append(services.pay_url)
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
            "link": services.current_link(invoice) if on else None,
            "received": received,
            "waiting": any(r.status == OnlinePayment.Status.WAITING for r in received),
            "can_manage": access.has_perm(request.actor, "payments.manage"),
            "payable": invoice.status in OPEN and invoice.balance > 0,
        },
        order=20,
    )
