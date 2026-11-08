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
    from trammel.core import templating

    notify(
        "payment_recorded",
        to=[payment.customer.email],
        entity=payment,
        context={
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
