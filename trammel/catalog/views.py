from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from trammel.core import access, archive, audit, panels
from trammel.core.services import save_audited
from trammel.core.web import csv_response, paginate, permission_required

from .models import Product


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "name",
            "sku",
            "kind",
            "description",
            "price",
            "cost",
            "unit",
            "tax_category",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def clean_sku(self):
        sku = self.cleaned_data["sku"].strip()
        if sku and Product.objects.filter(sku=sku).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another product already has this SKU.")
        return sku


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
        product = save_audited(form.save(commit=False))
        messages.success(request, f"{product} saved.")
        return redirect(product)
    return render(request, "catalog/form.html", {"form": form, "product": None})


@permission_required("catalog.view")
def product_detail(request, pk):
    """The product page: its details (a form for those who can edit), then
    other modules' tabs (stock adds the bill of materials) and files."""
    product = access.get_or_404(request.actor, "catalog.view", Product.objects.all(), pk=pk)
    can_edit = access.has_perm(request.actor, "catalog.edit", product)
    form = None
    if can_edit:
        before = audit.snapshot(product)
        form = ProductForm(request.POST or None, instance=product)
        if request.method == "POST" and form.is_valid():
            save_audited(form.save(commit=False), before=before)
            messages.success(request, f"{product} saved.")
            return redirect(product)
    elif request.method == "POST":
        access.require(request.actor, "catalog.edit")
    return render(
        request,
        "catalog/form.html",
        {
            "form": form,
            "product": product,
            "can_edit": can_edit,
            "can_costs": access.has_perm(request.actor, "core.see_costs"),
            **panels.tabbed(
                request,
                panels.for_object(request, product),
                first=(("details", "Details"),),
                last=(("files", "Files"),),
            ),
        },
    )


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
        parts="its bill of materials and files",
    )
