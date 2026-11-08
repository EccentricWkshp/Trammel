from __future__ import annotations

from django.contrib import messages
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import redirect, render

from trammel.core import access, audit
from trammel.core.services import save_audited
from trammel.core.web import permission_required

from .forms import MachineForm, MaterialForm, VendorForm
from .models import Machine, Material, Vendor

KINDS = {
    "machine": (Machine, MachineForm, "shop:machines"),
    "material": (Material, MaterialForm, "shop:materials"),
    "vendor": (Vendor, VendorForm, "shop:vendors"),
}


@permission_required("shop.view")
def machine_list(request):
    return render(
        request,
        "shop/machines.html",
        {
            "machines": Machine.objects.all(),
            "can_edit": access.has_perm(request.actor, "shop.edit"),
        },
    )


@permission_required("shop.view")
def material_list(request):
    qs = Material.objects.select_related("vendor")
    category = request.GET.get("category", "")
    if category:
        qs = qs.filter(category=category)
    if term := request.GET.get("q", "").strip():
        qs = (
            qs.filter(name__icontains=term)
            | qs.filter(sku__icontains=term)
            | qs.filter(vendor__name__icontains=term)
        )
    return render(
        request,
        "shop/materials.html",
        {
            "materials": qs,
            "categories": Material.Category.choices,
            "category": category,
            "q": term,
            "can_edit": access.has_perm(request.actor, "shop.edit"),
        },
    )


@permission_required("shop.view")
def vendor_list(request):
    qs = Vendor.objects.annotate(
        material_count=Count("materials", filter=Q(materials__deleted_at=None))
    )
    if term := request.GET.get("q", "").strip():
        qs = qs.filter(Q(name__icontains=term) | Q(contact_name__icontains=term))
    return render(
        request,
        "shop/vendors.html",
        {"vendors": qs, "q": term, "can_edit": access.has_perm(request.actor, "shop.edit")},
    )


@permission_required("shop.edit")
def edit(request, kind, pk=None):
    if kind not in KINDS:
        raise Http404
    model, form_class, back = KINDS[kind]
    obj = (
        access.get_or_404(request.actor, "shop.edit", model.objects.all(), pk=pk) if pk else model()
    )
    before = audit.snapshot(obj) if pk else None
    initial = {"vendor": request.GET.get("vendor")} if kind == "material" and not pk else None
    form = form_class(request.POST or None, instance=obj, initial=initial)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        if model is Machine and "use_suggested" in request.POST:
            suggested = obj.rate_breakdown().suggested
            if suggested:
                obj.hourly_rate = suggested
            save_audited(obj, before=before)
            messages.success(request, f"{obj} now charges {suggested} an hour.")
            return redirect(obj)
        save_audited(obj, before=before)
        messages.success(request, f"{obj} saved.")
        return redirect(back)
    noun = model._meta.verbose_name
    context = {"form": form, "title": f"Edit {obj}" if pk else f"Add {noun}", "back": back}
    if model is Machine:
        from trammel.core.models import ShopRates

        from .rates import PRESETS

        context |= {
            "breakdown": obj.rate_breakdown(),
            "electricity_rate": ShopRates.load().electricity_rate,
            "presets": PRESETS,
        }
        return render(request, "shop/machine_form.html", context)
    if model is Vendor and pk:
        context["materials"] = obj.materials.all()
    return render(request, f"shop/{kind}_form.html", context)
