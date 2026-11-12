"""Events raised by other modules' service hooks."""

from trammel.billing import services as billing
from trammel.quotes import services as quotes

from .services import notify


def _quote_ctx(revision):
    ctx = quotes.document_context(revision)
    return {**ctx, "customer": ctx["customer"]}


def quote_approved(revision, approval):
    notify(
        "quote_approved",
        context={**_quote_ctx(revision), "approved_by": approval.approved_by_name},
        entity=revision.quote,
    )


def quote_declined(revision, reason=""):
    notify(
        "quote_declined", context={**_quote_ctx(revision), "reason": reason}, entity=revision.quote
    )


def payment_recorded(payment):
    if not payment.customer.email or payment.amount <= 0:
        return
    # Paid through an online store: the store sends its own receipt, and its
    # buyers are never emailed from Trammel.
    if payment.method == payment.Method.ONLINE:
        return
    from trammel.core import templating

    # The invoices it paid, attached as they stand once it's applied.
    paid = []
    for allocation in payment.allocations.select_related("invoice").order_by("created_at"):
        if allocation.amount > 0 and allocation.invoice not in paid:
            paid.append(allocation.invoice)
    paid = paid[:5]
    notify(
        "payment_recorded",
        to=[payment.customer.email],
        entity=payment,
        attachment=[{"kind": "invoice", "id": str(i.pk)} for i in paid] or None,
        context={
            "invoices": [i.number for i in paid],
            "customer": templating.customer_context(payment.customer),
            "payment": {
                "amount": payment.amount,
                "received_on": payment.received_on,
                "method": payment.get_method_display(),
            },
            "credit": billing.customer_credit(payment.customer),
        },
    )


quotes.on_approved.append(quote_approved)
quotes.on_declined.append(quote_declined)
billing.on_payment.append(payment_recorded)
