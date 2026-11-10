"""Work orders open on the shop-floor page when a traveler is scanned or its number typed."""

from __future__ import annotations

from django.urls import reverse

from trammel.core import access, scan
from trammel.workorders.models import WorkOrder


def _visible(request, qs):
    return access.scoped(request.actor, "workorders.view", qs).first()


@scan.record
def work_order_by_id(request, pk):
    wo = _visible(request, WorkOrder.objects.filter(pk=pk))
    return reverse("floor:work_order", args=[wo.pk]) if wo else None


@scan.text
def work_order_by_number(request, code):
    if "/floor/wo/" in code:
        return code[code.index("/floor/wo/") :]
    wo = _visible(request, WorkOrder.objects.filter(number__iexact=code))
    return reverse("floor:work_order", args=[wo.pk]) if wo else None
