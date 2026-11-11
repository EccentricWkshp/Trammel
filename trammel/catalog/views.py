from __future__ import annotations

from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from trammel.core import access, archive, audit, panels
from trammel.core.web import csv_response, paginate, permission_required
from trammel.shop.models import Machine

from . import costing, services
from .models import Product, RoutingStep


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "name",
            "sku",
            "kind",
            "description",
            "price",
            "unit",
            "tax_category",
            "cost_override",
            "other_cost",
            "batch_size",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("batch_size", "other_cost"):
            if name in self.fields:
                self.fields[name].required = False

    def clean_other_cost(self):
        return self.cleaned_data.get("other_cost") or Decimal("0")

    def clean_batch_size(self):
        return self.cleaned_data.get("batch_size") or self.instance.batch_size or 1

    def clean_sku(self):
        sku = self.cleaned_data["sku"].strip()
        if sku and Product.objects.filter(sku=sku).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another product already has this SKU.")
        return sku


class RoutingStepForm(forms.ModelForm):
    class Meta:
        model = RoutingStep
        fields = [
            "name",
            "machine",
            "setup_minutes",
            "machine_minutes",
            "labor_minutes",
            "wait_hours",
            "instructions",
        ]
        labels = {
            "setup_minutes": "Setup (minutes, once per batch)",
            "machine_minutes": "Machine time (minutes each)",
            "labor_minutes": "Hands-on time (minutes each)",
        }
        widgets = {"instructions": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = self.instance.machine_id
        self.fields["machine"].queryset = Machine.objects.filter(is_active=True) | (
            Machine.objects.filter(pk=current) if current else Machine.objects.none()
        )
        self.fields["machine"].empty_label = "No machine"
        self.fields["wait_hours"].required = False

    def clean_wait_hours(self):
        """Blank for no wait."""
        return self.cleaned_data.get("wait_hours") or Decimal("0")


def _products(request):
    qs = Product.objects.select_related("tax_category")
    if term := request.GET.get("q", "").strip():
        qs = qs.filter(Product.search_filter(term))
    if request.GET.get("inactive") != "1":
        qs = qs.filter(is_active=True)
    return access.scoped(request.actor, "catalog.view", qs)


@permission_required("catalog.view")
def product_list(request):
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "catalog/list.html",
        {
            "page": paginate(request, _products(request)),
            "q": request.GET.get("q", ""),
            "query": params.urlencode(),
            "can_edit": access.has_perm(request.actor, "catalog.edit"),
        },
    )


@permission_required("catalog.view")
def product_csv(request):
    audit.record("export", entity_type="catalog.product", summary="Exported products as CSV")
    rows = (
        (
            p.sku,
            p.name,
            p.get_kind_display(),
            p.price,
            p.cost,
            p.unit,
            p.tax_category.name if p.tax_category else "",
            p.is_active,
        )
        for p in _products(request).iterator()
    )
    return csv_response(
        "products.csv",
        ["sku", "name", "kind", "price", "cost", "unit", "tax_category", "active"],
        rows,
    )


@permission_required("catalog.edit")
def product_create(request):
    form = ProductForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        product = services.save_product(form.save(commit=False))
        messages.success(request, f"{product} saved.")
        return redirect(product)
    return render(request, "catalog/form.html", {"form": form, "product": None})


@permission_required("catalog.view")
def product_detail(request, pk):
    """The product page: its details (a form for those who can edit), its
    routing, other modules' tabs (stock adds the bill of materials), what it
    costs, and files."""
    product = access.get_or_404(request.actor, "catalog.view", Product.objects.all(), pk=pk)
    can_edit = access.has_perm(request.actor, "catalog.edit", product)
    can_costs = access.has_perm(request.actor, "core.see_costs")
    form = None
    if can_edit:
        before = audit.snapshot(product)
        form = ProductForm(request.POST or None, instance=product)
        if not can_costs:
            for name in ("cost_override", "other_cost"):
                form.fields.pop(name)
        if request.method == "POST" and form.is_valid():
            services.save_product(form.save(commit=False), before=before)
            messages.success(request, f"{product} saved.")
            return redirect(product)
    elif request.method == "POST":
        access.require(request.actor, "catalog.edit")
    steps = list(product.routing_steps.select_related("machine"))
    context = {
        "form": form,
        "product": product,
        "can_edit": can_edit,
        "can_costs": can_costs,
        **panels.tabbed(
            request,
            panels.for_object(request, product),
            first=(("details", "Details"), ("routing", "Routing")),
            last=(*([("cost", "Cost")] if can_costs else []), ("files", "Files")),
        ),
    }
    for tab in context["tabs"]:
        if tab["key"] == "routing":
            tab["count"] = len(steps) or None
    if context["tab"] == "routing":
        context.update(_routing_context(request, product, steps, can_edit, can_costs))
    elif context["tab"] == "cost":
        parts = costing.parts(product)
        context.update({"parts": parts, "calculated": costing.calculated(product, parts)})
    return render(request, "catalog/form.html", context)


def _routing_context(request, product, steps, can_edit, can_costs) -> dict:
    labor = costing.labor_rate()
    batch = product.batch_size
    for step in steps:
        cost = costing.step_costs(step, batch, labor)
        step.unit_cost = sum(cost.values(), Decimal("0"))
    editing = None
    if can_edit and (pk := request.GET.get("edit")):
        editing = next((s for s in steps if str(s.pk) == pk), None)
    return {
        "steps": steps,
        "editing": editing,
        "step_form": RoutingStepForm(instance=editing) if can_edit else None,
        "unit_minutes": {
            "setup": sum((s.setup_minutes for s in steps), Decimal("0")),
            "labor": sum((s.labor_minutes for s in steps), Decimal("0")),
            "machine": sum((s.machine_minutes for s in steps), Decimal("0")),
        },
        "batch": batch,
        "wait_hours": sum((s.wait_hours for s in steps), Decimal("0")),
        "production": services.production_time(product, steps=steps),
        "routing_cost": sum((s.unit_cost for s in steps), Decimal("0")) if can_costs else None,
    }


def _step_errors(request, exc: ValidationError) -> None:
    for message in exc.messages:
        messages.error(request, message)


@permission_required("catalog.edit")
@require_POST
def step_save(request, pk, step_pk=None):
    """Add a step to the product's routing, or change one."""
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=pk)
    step = get_object_or_404(product.routing_steps, pk=step_pk) if step_pk else None
    form = RoutingStepForm(request.POST, instance=step or RoutingStep(product=product))
    url = f"{product.get_absolute_url()}?tab=routing"
    if not form.is_valid():
        for field, errors in form.errors.items():
            label = form.fields[field].label if field in form.fields else ""
            messages.error(request, f"{label}: {' '.join(errors)}" if label else " ".join(errors))
        return redirect(f"{url}&edit={step_pk}" if step_pk else url)
    try:
        services.save_step(form.save(commit=False))
    except ValidationError as exc:
        _step_errors(request, exc)
        return redirect(f"{url}&edit={step_pk}" if step_pk else url)
    return redirect(url)


@permission_required("catalog.edit")
@require_POST
def step_move(request, pk, step_pk):
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=pk)
    step = get_object_or_404(product.routing_steps, pk=step_pk)
    services.move_step(step, -1 if request.POST.get("direction") == "up" else 1)
    return redirect(f"{product.get_absolute_url()}?tab=routing")


@permission_required("catalog.edit")
@require_POST
def step_delete(request, pk, step_pk):
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=pk)
    services.delete_step(get_object_or_404(product.routing_steps, pk=step_pk))
    return redirect(f"{product.get_absolute_url()}?tab=routing")


@permission_required("catalog.edit")
@require_POST
def product_archive(request, pk):
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=pk)
    return archive.archive_view(request, product)


@permission_required("catalog.edit")
def product_delete(request, pk):
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=pk)
    return archive.delete_view(
        request,
        product,
        archive_url=reverse("catalog:archive", args=[pk]),
        done_url=reverse("catalog:list"),
        parts="its routing, bill of materials and files",
    )
