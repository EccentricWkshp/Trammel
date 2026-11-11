"""The imported order's place on its sale's page."""

from __future__ import annotations

from trammel.core import access, panels
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
