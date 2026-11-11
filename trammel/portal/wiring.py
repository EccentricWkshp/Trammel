"""The portal's place on other modules' pages and in their emails."""

from __future__ import annotations

from django.urls import reverse

from trammel.core import access, panels
from trammel.notifications import hooks as email_hooks

from . import services
from .models import Access


@panels.register("customers.customer")
def customer_portal(request, customer):
    """Who at the customer can use the portal, and the switch for it."""
    if customer.is_internal or customer.is_placeholder:
        return None
    if not access.has_perm(request.actor, "customers.view", customer):
        return None
    row = Access.objects.filter(customer=customer).first()
    return panels.Panel(
        "Customer portal",
        "portal/_customer_panel.html",
        {
            "access": row,
            "enabled": bool(row and row.enabled),
            "signers": services.signers_of(customer),
            "can_manage": access.has_perm(request.actor, "portal.manage"),
            "email_ready": services.email_ready(),
            "portal_url": services.link(),
        },
        order=90,
    )


@email_hooks.online_links.append
def portal_link(record) -> str:
    """A link to a quote or invoice in the portal, for its email, when the
    customer can see it there."""
    if not services.is_on():
        return ""
    from .views import SHOWN_INVOICES, SHOWN_QUOTES

    meta = record._meta.label_lower
    if meta == "quotes.quoterevision":
        customer, shown = record.quote.job.customer, record.status in SHOWN_QUOTES
        name = "portal:quote"
    elif meta == "billing.invoice":
        customer, shown = record.customer, record.status in SHOWN_INVOICES
        name = "portal:invoice"
    else:
        return ""
    if not shown or not services.has_access(customer):
        return ""
    return services.link(reverse(name, args=[record.pk]))
