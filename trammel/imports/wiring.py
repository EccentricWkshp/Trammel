"""The imported order's place on its sale's page."""

from __future__ import annotations

from django.urls import reverse

from trammel.core import access, modules, notices, panels
from trammel.notifications.services import reminder_filters

from .models import ImportedOrder

# An online store's buyers are never emailed from Trammel: the store
# collects its own orders, so its sales get no overdue reminders. (Their
# payments, by "Online store", send no receipt either.)
reminder_filters.append(lambda invoices: invoices.filter(imported_order__isnull=True))


@panels.register("billing.invoice")
def imported_from(request, invoice):
    row = ImportedOrder.objects.filter(invoice=invoice).select_related("job").first()
    if row is None:
        return None
    return panels.Panel(
        "Online order",
        "imports/_invoice_panel.html",
        {
            "row": row,
            "can_make_job": row.job_id is None
            and access.has_perm(request.actor, "imports.run")
            and access.has_perm(request.actor, "jobs.create"),
        },
        order=5,
    )


@notices.register
def deletion_requests(request):
    """A platform's request to delete a buyer's data waits for staff (FR-IMP-6)."""
    actor = getattr(request, "actor", None)
    if not (modules.is_enabled("imports") and access.has_perm(actor, "imports.manage")):
        return []
    from .deletions import open_count

    waiting = open_count()
    if not waiting:
        return []
    return [
        notices.Notice(
            "warning",
            f"{waiting} request{'s' if waiting != 1 else ''} from eBay to delete a buyer's "
            f"data {'are' if waiting != 1 else 'is'} waiting for review.",
            reverse("imports:deletions"),
        )
    ]
