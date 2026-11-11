from __future__ import annotations

import base64
from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.http import content_disposition_header, url_has_allowed_host_and_scheme
from django.utils.timezone import localtime, now
from django.views.decorators.http import require_POST

from trammel.core import access, audit, modules, panels, templating, units
from trammel.core.lifecycle import TransitionError
from trammel.core.money import MoneyInput
from trammel.core.services import save_audited
from trammel.core.web import (
    document_response,
    document_view,
    paginate,
    permission_required,
    safe_next,
)
from trammel.jobs.models import Job
from trammel.shop.models import Machine, Material

from . import hooks, services
from .models import Operation, WorkOrder


class WorkOrderForm(forms.ModelForm):
    class Meta:
        model = WorkOrder
        fields = ["title", "instructions", "target_completion"]
        widgets = {
            "target_completion": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "instructions": forms.Textarea(attrs={"rows": 4}),
        }


class MaterialUseForm(forms.Form):
    material = forms.ModelChoiceField(Material.objects.filter(is_active=True), required=False)
    description = forms.CharField(required=False, help_text="For something not on the list.")
    quantity = forms.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text="Negative to correct an earlier entry, or to put stock back.",
    )
    unit = forms.ChoiceField(
        choices=[("", "— material's own unit —"), *units.UNIT_CHOICES], required=False
    )
    unit_cost = MoneyInput(
        max_digits=14,
        decimal_places=4,
        required=False,
        help_text="Only for items not on the list. Stocked materials cost what their lots cost.",
    )
    lot = forms.CharField(
        required=False,
        max_length=200,
        help_text="For a material kept in stock: a lot number, or scan its label. "
        "Otherwise the oldest is used.",
    )
    leftover_length = forms.DecimalField(
        label="Leftover piece: length",
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=4,
        help_text="A piece of what was used that goes back into stock as a remnant. "
        "The work order pays only for the rest.",
    )
    leftover_width = forms.DecimalField(
        label="width",
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=4,
        help_text="For sheet goods. Leave it out for boards and bars.",
    )
    leftover_unit = forms.ChoiceField(
        label="in",
        choices=[("in", "in"), ("ft", "ft"), ("mm", "mm"), ("cm", "cm"), ("m", "m")],
        required=False,
    )
    billable = forms.BooleanField(required=False, initial=True)

    STOCK_ONLY = ("lot", "leftover_length", "leftover_width", "leftover_unit")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not hooks.lots_offered():
            for name in self.STOCK_ONLY:
                del self.fields[name]

    def leftover(self):
        d = self.cleaned_data
        if d.get("leftover_length"):
            return (d["leftover_length"], d.get("leftover_width"), d.get("leftover_unit") or "in")
        return None


def _wo(request, perm, pk) -> WorkOrder:
    return access.get_or_404(
        request.actor, perm, WorkOrder.objects.select_related("job__customer"), pk=pk
    )


def floor_url(wo: WorkOrder) -> str:
    return settings.BASE_URL + reverse("floor:work_order", args=[wo.pk])


def qr_svg(data: str) -> str:
    from trammel.core.qr import svg

    return svg(data)


@permission_required("workorders.view")
def wo_list(request):
    qs = WorkOrder.objects.select_related("job__customer")
    status = request.GET.get("status", "active")
    if status == "active":
        qs = qs.filter(status__in=["open", "in_progress", "waiting"])
    elif status:
        qs = qs.filter(status=status)
    qs = access.scoped(request.actor, "workorders.view", qs).order_by(
        "target_completion", "created_at"
    )
    return render(
        request,
        "workorders/list.html",
        {"page": paginate(request, qs), "status": status, "query": f"status={status}"},
    )


@permission_required("workorders.create")
@require_POST
def wo_create(request, job_pk):
    job = access.get_or_404(request.actor, "jobs.view", Job.objects.all(), pk=job_pk)
    revision = None
    if rev_pk := request.POST.get("revision"):
        from trammel.quotes.models import QuoteRevision

        revision = QuoteRevision.objects.filter(
            pk=rev_pk, quote__job=job, quote__kind="quote", status="approved"
        ).first()
    try:
        wo = services.create(job, title=request.POST.get("title", ""), revision=revision)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(job)
    messages.success(request, f"Work order {wo.number} created.")
    return redirect(wo)


@permission_required("workorders.create")
def wo_new(request):
    """Start a work order from the work orders page: pick a job, then a quote."""
    from trammel.quotes.models import QuoteRevision

    candidates = access.scoped(
        request.actor,
        "jobs.view",
        Job.objects.filter(status__in=("approved", "in_progress")).select_related("customer"),
    ).order_by("-created_at")
    ready = [j for j in candidates if not services.can_create_for(j)]
    job = next((j for j in ready if str(j.pk) == request.GET.get("job")), None)
    approved = (
        QuoteRevision.objects.filter(
            quote__job=job, quote__kind="quote", status="approved"
        ).select_related("quote")
        if job
        else []
    )
    return render(request, "workorders/new.html", {"jobs": ready, "job": job, "approved": approved})


@permission_required("workorders.view")
def wo_detail(request, pk):
    wo = _wo(request, "workorders.view", pk)
    actor = request.actor
    can_edit = access.has_perm(actor, "workorders.edit", wo) and wo.is_editable
    operations = services.operation_tree(wo)
    finish = services.estimate_finish(wo, operations)
    if can_edit:
        options = services.parent_options(operations)
        for op in operations:
            op.parent_options = options[op.pk]
    return render(
        request,
        "workorders/detail.html",
        {
            "wo": wo,
            "job": wo.job,
            "operations": operations,
            "items": wo.items.select_related("completed_by", "source_revision__quote"),
            "finish": finish,
            "finish_late": _late(wo, finish),
            "can_nest": [op for op in operations if op.depth < Operation.MAX_DEPTH],
            "done_count": wo.operations.exclude(status="pending").count(),
            "items_left": wo.items.filter(status="pending").count(),
            "planned": hooks.annotate_planned(
                wo.planned_materials.select_related("material", "operation")
            ),
            "ready_to_record": list(services.ready_to_record(wo))
            if can_edit and hooks.planned_use_mode() != "off"
            else [],
            "usage": wo.material_use.select_related("material", "recorded_by"),
            "notes": wo.notes.select_related("author", "document__current__blob"),
            # Sharing notes with the customer only means something with the portal on.
            "portal_on": modules.is_enabled("portal"),
            "actions": [t for t in WorkOrder.lifecycle.available(wo, actor)],
            "can_edit": can_edit,
            "can_costs": access.has_perm(actor, "core.see_costs"),
            "material_form": MaterialUseForm(),
            "machines": Machine.objects.filter(is_active=True),
            "waiting_choices": WorkOrder.WaitingOn.choices,
            **panels.tabbed(
                request,
                panels.for_object(request, wo),
                first=(("operations", "Operations"), ("materials", "Materials")),
                last=(("notes", "Notes and photos"), ("files", "Files")),
            ),
        },
    )


@permission_required("workorders.edit")
def wo_edit(request, pk):
    wo = _wo(request, "workorders.edit", pk)
    before = audit.snapshot(wo)
    form = WorkOrderForm(request.POST or None, instance=wo)
    if request.method == "POST" and form.is_valid():
        save_audited(form.save(commit=False), before=before)
        return redirect(wo)
    return render(request, "workorders/form.html", {"form": form, "wo": wo})


@permission_required("workorders.view")
@require_POST
def wo_transition(request, pk):
    wo = _wo(request, "workorders.view", pk)
    try:
        services.transition(
            wo,
            request.POST.get("target", ""),
            actor=request.actor,
            reason=request.POST.get("reason", ""),
            waiting_on=request.POST.get("waiting_on", ""),
        )
    except TransitionError as exc:
        for p in exc.problems:
            messages.error(request, p)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    except PermissionDenied:
        messages.error(request, "Your role does not allow that.")
    return redirect(safe_next(request, wo.get_absolute_url()))


@permission_required("workorders.edit")
@require_POST
def op_set(request, pk, op_pk):
    wo = _wo(request, "workorders.edit", pk)
    op = wo.operations.filter(pk=op_pk).first()
    if op is None:
        raise Http404
    try:
        services.set_operation(op, request.POST.get("state", ""), user=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(safe_next(request, wo.get_absolute_url()))


@permission_required("workorders.edit")
@require_POST
def item_set(request, pk, item_pk):
    """Check an item to include off: included, not needed, or back to include."""
    wo = _wo(request, "workorders.edit", pk)
    item = wo.items.filter(pk=item_pk).first()
    if item is None:
        raise Http404
    try:
        services.set_item(item, request.POST.get("state", ""), user=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(safe_next(request, wo.get_absolute_url()))


@permission_required("workorders.edit")
@require_POST
def item_delete(request, pk, item_pk):
    wo = _wo(request, "workorders.edit", pk)
    item = wo.items.filter(pk=item_pk).first()
    if item is None:
        raise Http404
    try:
        services.delete_item(item)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"Removed “{item.description}”.")
    return redirect(_ops_tab(wo))


def _op(request, pk, op_pk):
    wo = _wo(request, "workorders.edit", pk)
    op = wo.operations.filter(pk=op_pk).first()
    if op is None:
        raise Http404
    return wo, op


def _parent(wo, value: str):
    """The operation picked as a parent on a form; blank is the top level."""
    if not value:
        return None
    try:
        parent = wo.operations.filter(pk=value).first()
    except ValidationError:
        parent = None
    if parent is None:
        raise ValidationError("That operation is not on this work order.")
    return parent


def _late(wo, finish) -> tuple[str, object] | None:
    """The first date the estimated finish misses: (what it is, the date)."""
    if finish is None:
        return None
    day = localtime(finish).date()
    job = wo.job
    for label, date in (
        ("the job's promised date", job.promised_date),
        ("the job's due date", job.due_date),
        ("the work order's target", wo.target_completion),
    ):
        if date and day > date:
            return label, date
    return None


def _ops_tab(wo):
    return f"{wo.get_absolute_url()}?tab=operations"


@permission_required("workorders.edit")
@require_POST
def op_edit(request, pk, op_pk):
    wo, op = _op(request, pk, op_pk)
    machine = Machine.objects.filter(pk=request.POST.get("machine") or None).first()
    try:
        with transaction.atomic():
            services.update_operation(
                op,
                name=request.POST.get("name", ""),
                description=request.POST.get("description", ""),
                machine=machine,
                planned_labor_minutes=_minutes(request.POST.get("planned_labor_minutes")),
                planned_machine_minutes=_minutes(request.POST.get("planned_machine_minutes")),
                planned_wait_hours=_minutes(
                    request.POST.get("planned_wait_hours"), "Enter the wait in hours, such as 12."
                ),
            )
            if "parent" in request.POST:
                services.reparent_operation(
                    op, _parent(wo, request.POST["parent"]), user=request.user
                )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(_ops_tab(wo))


def _minutes(value: str | None, problem: str = "Enter planned time in minutes, such as 45."):
    """Planned minutes (or hours) typed on the form: blank for none."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except ArithmeticError:
        raise ValidationError(problem) from None


@permission_required("workorders.edit")
@require_POST
def op_move(request, pk, op_pk):
    wo, op = _op(request, pk, op_pk)
    try:
        services.move_operation(op, -1 if request.POST.get("direction") == "up" else 1)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(_ops_tab(wo))


@permission_required("workorders.edit")
@require_POST
def op_delete(request, pk, op_pk):
    wo, op = _op(request, pk, op_pk)
    name = op.name
    try:
        detached = services.delete_operation(op)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        note = (
            f" Its {detached} time entr{'y' if detached == 1 else 'ies'} stay on the work order."
            if detached
            else ""
        )
        messages.success(request, f"Deleted “{name}”.{note}")
    return redirect(_ops_tab(wo))


@permission_required("workorders.edit")
@require_POST
def op_add(request, pk):
    wo = _wo(request, "workorders.edit", pk)
    machine = Machine.objects.filter(pk=request.POST.get("machine") or None).first()
    name = request.POST.get("name", "").strip()
    if not name:
        messages.error(request, "Name the operation.")
    else:
        try:
            services.add_operation(
                wo,
                name=name,
                machine=machine,
                description=request.POST.get("description", ""),
                parent=_parent(wo, request.POST.get("parent", "")),
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    return redirect(_ops_tab(wo))


@permission_required("workorders.edit")
@require_POST
def note_add(request, pk):
    wo = _wo(request, "workorders.edit", pk)
    try:
        services.add_note(
            wo,
            text=request.POST.get("text", ""),
            upload=request.FILES.get("photo"),
            user=request.user,
            shared=request.POST.get("shared") == "1",
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(safe_next(request, wo.get_absolute_url()))


@permission_required("workorders.edit")
@require_POST
def note_share(request, pk, note_pk):
    """Show a note to the customer in the portal, or stop (FR-PRT-4)."""
    wo = _wo(request, "workorders.edit", pk)
    note = get_object_or_404(wo.notes.all(), pk=note_pk)
    services.share_note(note, request.POST.get("shared") == "1")
    return redirect(f"{wo.get_absolute_url()}?tab=notes")


@permission_required("workorders.edit")
@require_POST
def material_add(request, pk):
    wo = _wo(request, "workorders.edit", pk)
    form = MaterialUseForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        try:
            services.record_material(
                wo,
                material=d["material"],
                quantity=d["quantity"],
                unit=d["unit"],
                description=d["description"],
                unit_cost=d["unit_cost"],
                billable=d["billable"],
                lot=d.get("lot") or None,
                leftover=form.leftover(),
                user=request.user,
            )
        except (ValidationError, units.IncompatibleUnits) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
    else:
        messages.error(request, "Enter a quantity, and a material or a description and cost.")
    return redirect(safe_next(request, wo.get_absolute_url()))


@permission_required("workorders.edit")
@require_POST
def planned_use(request, pk):
    """Record the planned materials of done operations as used, as planned."""
    wo = _wo(request, "workorders.edit", pk)
    ready = services.ready_to_record(wo)
    chosen = set(request.POST.getlist("planned"))
    if chosen:
        ready = [p for p in ready if str(p.pk) in chosen]
    try:
        usages = services.use_planned(wo, ready, user=request.user)
    except (ValidationError, units.IncompatibleUnits) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
    else:
        messages.success(
            request, f"Recorded {len(usages)} material use{'s' if len(usages) != 1 else ''}."
        )
    back = request.POST.get("next", "")
    if back and url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        return redirect(back)
    return redirect(f"{wo.get_absolute_url()}?tab=materials")


@permission_required("workorders.edit")
@require_POST
def materials_confirm(request, pk):
    wo = _wo(request, "workorders.edit", pk)
    services.confirm_materials(wo, request.POST.get("confirmed") == "1")
    return redirect(wo)


def _traveler_context(request, wo: WorkOrder) -> dict:
    from trammel.files.services import attachments_for, media_root

    images, files = [], []
    for link in [*attachments_for(wo.job), *attachments_for(wo)]:
        doc = link.document
        # Files with a type are printed only when the type says so; files
        # with none are printed as they always were.
        if doc.doc_type is not None and not doc.doc_type.show_on_traveler:
            continue
        blob = doc.current.blob if doc.current else None
        if blob and not blob.thumbnail:
            files.append(
                {
                    "title": doc.title,
                    "type": doc.doc_type.name if doc.doc_type else "",
                    "name": doc.current.original_name,
                    "revision": doc.current.number,
                }
            )
        if blob and blob.thumbnail:
            data = (media_root() / blob.thumbnail).read_bytes()
            images.append(
                {
                    "title": link.document.title,
                    "src": "data:image/webp;base64," + base64.b64encode(data).decode(),
                }
            )
    url = floor_url(wo)
    return {
        "wo": wo,
        "job": wo.job,
        "operations": services.operation_tree(wo),
        "blank_rows": range(6),
        "items": wo.items.select_related("completed_by"),
        "planned": hooks.annotate_planned(wo.planned_materials.select_related("material")),
        "notes": wo.notes.filter(document__isnull=True).select_related("author")[:10],
        "printed_at": now(),
        "images": images,
        "files": files,
        "qr": qr_svg(url),
        "url": url,
        "business": request.business,
    }


@permission_required("workorders.view")
@document_view
def traveler(request, pk):
    """The job traveler (FR-WO-4): no pricing, a QR code to the shop-floor page."""
    wo = _wo(request, "workorders.view", pk)
    html = render_to_string(
        "workorders/traveler.html", _traveler_context(request, wo), request=request
    )
    if request.GET.get("format") == "pdf":
        try:
            data = templating.render_pdf(html)
        except templating.PdfUnavailable as exc:
            messages.error(request, str(exc))
            return redirect(wo)
        response = HttpResponse(data, content_type="application/pdf")
        response["Content-Disposition"] = content_disposition_header(
            True, f"traveler-{wo.number}.pdf"
        )
        return response
    return document_response(request, html)


@permission_required("workorders.edit")
@require_POST
def add_change_order(request, pk):
    """Add an approved change order of the job to this work order (FR-WO-5)."""
    from trammel.quotes.models import QuoteRevision

    wo = _wo(request, "workorders.edit", pk)
    revision = QuoteRevision.objects.filter(
        pk=request.POST.get("revision") or None, quote__job=wo.job
    ).first()
    if revision is None:
        raise Http404
    try:
        services.add_change_order(wo, revision)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"{revision.quote.number} added to {wo.number}.")
    return redirect(reverse("workorders:detail", args=[wo.pk]) + "?tab=change-orders")
