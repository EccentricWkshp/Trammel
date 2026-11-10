from __future__ import annotations

import base64

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.timezone import now
from django.views.decorators.http import require_POST

from trammel.core import access, audit, panels, templating, units
from trammel.core.lifecycle import TransitionError
from trammel.core.money import MoneyInput
from trammel.core.services import save_audited
from trammel.core.web import document_response, document_view, paginate, permission_required
from trammel.jobs.models import Job
from trammel.shop.models import Machine, Material

from . import hooks, services
from .models import ChangeOrder, ChangeOrderLine, COStatus, Operation, WorkOrder


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


class ChangeOrderLineForm(forms.ModelForm):
    class Meta:
        model = ChangeOrderLine
        fields = [
            "description",
            "quantity",
            "unit",
            "unit_price",
            "unit_cost",
            "category",
            "tax_category",
        ]


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
            pk=rev_pk, quote__job=job, status="approved"
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
        QuoteRevision.objects.filter(quote__job=job, status="approved").select_related("quote")
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
            "can_nest": [op for op in operations if op.depth < Operation.MAX_DEPTH],
            "done_count": wo.operations.exclude(status="pending").count(),
            "planned": hooks.annotate_planned(wo.planned_materials.select_related("material")),
            "usage": wo.material_use.select_related("material", "recorded_by"),
            "notes": wo.notes.select_related("author", "document__current__blob"),
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
    return redirect(request.POST.get("next") or wo.get_absolute_url())


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
    return redirect(request.POST.get("next") or wo.get_absolute_url())


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
            )
            if "parent" in request.POST:
                services.reparent_operation(
                    op, _parent(wo, request.POST["parent"]), user=request.user
                )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(_ops_tab(wo))


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
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(request.POST.get("next") or wo.get_absolute_url())


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
    return redirect(request.POST.get("next") or wo.get_absolute_url())


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
        response["Content-Disposition"] = f'attachment; filename="traveler-{wo.number}.pdf"'
        return response
    return document_response(request, html)


# ---------------------------------------------------------------------------
# Change orders
# ---------------------------------------------------------------------------


def _co(request, perm, pk) -> ChangeOrder:
    return access.get_or_404(
        request.actor, perm, ChangeOrder.objects.select_related("job__customer"), pk=pk
    )


@permission_required("workorders.edit")
@require_POST
def co_create(request, job_pk):
    job = access.get_or_404(request.actor, "jobs.view", Job.objects.all(), pk=job_pk)
    try:
        co = services.create_change_order(
            job, title=request.POST.get("title", "").strip() or "Change order"
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(job)
    return redirect(co)


@permission_required("workorders.view")
def co_detail(request, pk):
    co = _co(request, "workorders.view", pk)
    draft = co.status == COStatus.DRAFT and access.has_perm(request.actor, "workorders.edit", co)
    form = ChangeOrderLineForm(request.POST or None) if draft else None
    if request.method == "POST" and draft:
        if "description_text" in request.POST:
            before = audit.snapshot(co)
            co.description = request.POST.get("description_text", "")
            co.amended_terms = request.POST.get("amended_terms", "")
            save_audited(co, before=before)
            return redirect(co)
        if form.is_valid():
            line = form.save(commit=False)
            line.change_order = co
            services.save_co_line(line)
            return redirect(co)
    return render(
        request,
        "workorders/change_order.html",
        {
            "co": co,
            "lines": co.lines.all(),
            "form": form,
            "draft": draft,
            "can_approve": co.status == COStatus.SENT
            and access.has_perm(request.actor, "workorders.approve", co),
            "can_send": draft,
        },
    )


@permission_required("workorders.edit")
@require_POST
def co_line_delete(request, pk, line_pk):
    co = _co(request, "workorders.edit", pk)
    line = co.lines.filter(pk=line_pk).first()
    if line is None:
        raise Http404
    try:
        services.delete_co_line(line)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect(co)


@permission_required("workorders.view")
@require_POST
def co_action(request, pk):
    co = _co(request, "workorders.view", pk)
    action = request.POST.get("action")
    try:
        if action == "send":
            ChangeOrder.lifecycle.apply(co, COStatus.SENT, actor=request.actor)
        elif action == "decline":
            ChangeOrder.lifecycle.apply(
                co, COStatus.DECLINED, actor=request.actor, reason=request.POST.get("reason", "")
            )
        elif action == "approve":
            services.approve_change_order(
                co,
                actor=request.actor,
                approved_by_name=request.POST.get("approved_by_name", ""),
                how=request.POST.get("how", ""),
                user=request.user,
            )
        else:
            raise Http404
    except TransitionError as exc:
        messages.error(request, " ".join(exc.problems))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    except PermissionDenied:
        messages.error(request, "Your role does not allow that.")
    return redirect(co)
