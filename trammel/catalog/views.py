from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from trammel.core import access, audit, panels
from trammel.core.services import save_audited
from trammel.core.web import csv_response, paginate, permission_required

from . import services
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
def product_edit(request, pk=None):
    obj = (
        access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=pk)
        if pk
        else Product()
    )
    before = audit.snapshot(obj) if pk else None
    form = ProductForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        save_audited(form.save(commit=False), before=before)
        messages.success(request, f"{obj} saved.")
        return redirect(obj)
    if not pk:
        return render(request, "catalog/form.html", {"form": form, "product": None})
    return render(
        request,
        "catalog/form.html",
        {
            "form": form,
            "product": obj,
            "can_delete": access.has_perm(request.actor, "catalog.delete", obj),
            # Other modules' tabs: stock adds the bill of materials.
            **panels.tabbed(
                request,
                panels.for_object(request, obj),
                first=(("details", "Details"),),
                last=(("files", "Files"),),
            ),
        },
    )


@permission_required("catalog.edit")
@require_POST
def product_archive(request, pk):
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=pk)
    archived = request.POST.get("archived") == "1"
    services.set_archived(product, archived)
    if archived:
        messages.success(request, f"{product} archived. It can't be picked for new quotes.")
    else:
        messages.success(request, f"{product} is back in the catalog.")
    return redirect(product)


@permission_required("catalog.delete")
def product_delete(request, pk):
    product = access.get_or_404(request.actor, "catalog.delete", Product.objects.all(), pk=pk)
    used = services.in_use(product)
    if request.method == "POST" and not used:
        try:
            services.delete_product(product, reason=request.POST.get("reason", ""))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect(product)
        messages.success(request, f"{product} moved to the trash. It can be restored for 30 days.")
        return redirect("catalog:list")
    return render(request, "catalog/confirm_delete.html", {"product": product, "used": used})
