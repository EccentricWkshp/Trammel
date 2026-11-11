"""The portal's place on other modules' pages and in their emails."""

from __future__ import annotations

from django.urls import reverse

from trammel.core import access, dashboard, panels
from trammel.notifications import hooks as email_hooks

from . import services, uploads
from .models import Access, Upload


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
            "can_preview": services.can_preview(request.actor, customer),
            "waiting_code": services.waiting_code(customer) if row and row.enabled else None,
            "email_ready": services.email_ready(),
            "portal_url": services.link(),
        },
        order=90,
    )


def _sent_panel(request, obj, **lookup):
    if not access.has_perm(request.actor, "files.upload"):
        return None
    sent = list(
        Upload.objects.filter(**lookup)
        .select_related("job", "reviewed_by", "document__current__blob")
        .order_by("-sent_at")[:20]
    )
    if not sent:
        return None
    return panels.Panel(
        "Files the customer sent",
        "portal/_sent_panel.html",
        {"sent": sent, "waiting": sum(1 for u in sent if u.status == Upload.Status.PENDING)},
        order=85,
    )


@panels.register("customers.customer")
def customer_sent(request, customer):
    """Files the customer sent through the portal, for review (FR-PRT-7)."""
    if not access.has_perm(request.actor, "customers.view", customer):
        return None
    return _sent_panel(request, customer, customer=customer)


@panels.register("jobs.job")
def job_sent(request, job):
    if not access.has_perm(request.actor, "jobs.view", job):
        return None
    return _sent_panel(request, job, job=job)


@dashboard.register(
    "portal.uploads",
    "Files from customers",
    order=35,
    permission="files.upload",
    description="Files customers sent through the portal, waiting for you to accept or decline.",
)
def files_to_review(request):
    from trammel.customers.models import Customer

    found = uploads.waiting(
        Upload.objects.filter(
            customer__in=access.scoped(request.actor, "customers.view", Customer.objects.all())
        )
    ).order_by("sent_at")
    return dashboard.Section(
        template="portal/_home_uploads.html",
        rows=list(found[: dashboard.LIMIT]),
        total=found.count(),
        more_url=reverse("portal:review_list"),
        empty="No files waiting.",
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
