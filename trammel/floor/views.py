"""The shop-floor app (SPEC §8): phone/tablet pages over the same services."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.cache import cache_control

from trammel.core import access
from trammel.core.web import permission_required
from trammel.workorders import services as wo_services
from trammel.workorders.models import WorkOrder
from trammel.workorders.views import MaterialUseForm


@login_required
def home(request):
    from trammel.timetracking.services import running_for

    active = (
        access.scoped(
            request.actor,
            "workorders.view",
            WorkOrder.objects.filter(status__in=["in_progress", "waiting", "open"]),
        )
        .select_related("job__customer")
        .order_by("target_completion", "created_at")[:30]
    )
    running = (
        running_for(request.user).select_related("work_order").first()
        if access.has_perm(request.actor, "time.view")
        else None
    )
    return render(request, "floor/home.html", {"active": active, "running": running})


@login_required
def find(request):
    """Open whatever a scanned or typed code is (FR-STK-7): a label's link, a work
    order or lot number, or a product barcode."""
    from django.contrib import messages

    from trammel.core import scan as scanning

    q = request.GET.get("q", "").strip()
    if not q:
        messages.error(request, "Scan a code, or type a number.")
        return redirect("floor:scan")
    url = scanning.find(request, q)
    if url:
        return redirect(url)
    offer = scanning.offer(request, q)
    if offer:
        url, text = offer
        messages.info(request, text)
        return redirect(url)
    messages.error(request, f"Nothing matches “{q}”.")
    return redirect("floor:scan")


@login_required
def scan(request):
    return render(request, "floor/scan.html")


@permission_required("workorders.view")
def work_order(request, pk):
    wo = access.get_or_404(
        request.actor, "workorders.view", WorkOrder.objects.select_related("job__customer"), pk=pk
    )
    from trammel.files.services import attachments_for
    from trammel.timetracking.models import TimeEntry

    here = request.get_full_path()
    # Stock pages offer this work order first for “Use on work order”.
    request.session["floor_work_order"] = str(wo.pk)
    return render(
        request,
        "floor/work_order.html",
        {
            "wo": wo,
            "job": wo.job,
            "operations": wo_services.operation_tree(wo),
            "actions": [
                t
                for t in WorkOrder.lifecycle.available(wo, request.actor)
                if t.kind == "forward" or t.target == "in_progress"
            ],
            "waiting_choices": WorkOrder.WaitingOn.choices,
            "mine": TimeEntry.objects.filter(
                work_order=wo, user=request.user, ended_at__isnull=True
            ).first(),
            "running_machines": TimeEntry.objects.filter(
                work_order=wo, kind="machine", ended_at__isnull=True
            ).select_related("machine"),
            "machines": wo.operations.exclude(machine=None)
            .values_list("machine__pk", "machine__name")
            .distinct(),
            "drawings": [*attachments_for(wo.job), *attachments_for(wo)],
            "notes": wo.notes.select_related("author", "document")[:20],
            "material_form": MaterialUseForm(),
            "can_edit": access.has_perm(request.actor, "workorders.edit", wo) and wo.is_editable,
            "can_track": access.has_perm(request.actor, "time.track", wo) and wo.is_active,
            "next": here,
        },
    )


@cache_control(max_age=3600)
def manifest(request):
    import json

    data = {
        "name": f"{request.business.name} — shop floor" if request.business else "Trammel",
        "short_name": "Shop floor",
        "start_url": reverse("floor:home"),
        "scope": reverse("floor:home"),
        "display": "standalone",
        "background_color": "#f6f5f2",
        "theme_color": "#1f5f8b",
        "icons": [
            {"src": static("floor/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": static("floor/icon-512.png"), "sizes": "512x512", "type": "image/png"},
        ],
    }
    return HttpResponse(json.dumps(data), content_type="application/manifest+json")


@cache_control(no_cache=True)
def service_worker(request):
    """Served from /floor/ so its scope is the shop-floor app. No offline queue (§1.2)."""
    body = (settings.BASE_DIR / "static" / "floor" / "sw.js").read_text(encoding="utf-8")
    response = HttpResponse(body, content_type="application/javascript")
    response["Service-Worker-Allowed"] = reverse("floor:home")
    return response
