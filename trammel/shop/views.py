from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from trammel.core import access, archive, audit, panels
from trammel.core.services import save_audited
from trammel.core.web import permission_required

from . import hooks
from .forms import MachineForm, MaterialBarcodeForm, MaterialForm, MaterialUnitForm, VendorForm
from .models import Machine, Material, MaterialBarcode, MaterialUnit, Vendor

#: One permission per kind covers adding, editing, archiving and deleting it:
#: machines may be the shop's call, materials staff's, vendors an admin's.
PERMS = {
    "machine": "shop.manage_machines",
    "material": "shop.manage_materials",
    "vendor": "shop.manage_vendors",
}

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
            "can_edit": access.has_perm(request.actor, PERMS["machine"]),
        },
    )


@permission_required("shop.view")
def material_list(request):
    home = hooks.materials_home()
    if home != request.path:
        query = request.GET.urlencode()
        return redirect(f"{home}?{query}" if query else home)
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
            "can_edit": access.has_perm(request.actor, PERMS["material"]),
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
        {"vendors": qs, "q": term, "can_edit": access.has_perm(request.actor, PERMS["vendor"])},
    )


@permission_required("shop.view")
def material_detail(request, pk):
    material = access.get_or_404(
        request.actor,
        "shop.view",
        Material.objects.select_related("vendor").prefetch_related("conversions", "barcodes"),
        pk=pk,
    )
    can_edit = access.has_perm(request.actor, PERMS["material"])
    context = {
        **_archive_urls(request, "material", material),
        "material": material,
        "can_edit": can_edit,
        "can_costs": access.has_perm(request.actor, "core.see_costs"),
        "unit_options": material.unit_choices(),
        "materials_home": hooks.materials_home(),
        **panels.tabbed(request, panels.for_object(request, material)),
    }
    if can_edit:
        context["conversion_form"] = MaterialUnitForm(material=material)
        context["barcode_form"] = MaterialBarcodeForm(material=material)
    return render(request, "shop/material_detail.html", context)


def _material_tab(material) -> str:
    return f"{material.get_absolute_url()}#conversions"


@permission_required("shop.manage_materials")
@require_POST
def conversion_add(request, pk):
    material = access.get_or_404(request.actor, PERMS["material"], Material.objects.all(), pk=pk)
    form = MaterialUnitForm(request.POST, material=material)
    if form.is_valid():
        row = form.save()
        audit.record("material.conversion", material, summary=f"{material}: {row}")
        messages.success(request, f"Added {row}.")
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect(_material_tab(material))


@permission_required("shop.manage_materials")
@require_POST
def conversion_delete(request, pk, row_pk):
    material = access.get_or_404(request.actor, PERMS["material"], Material.objects.all(), pk=pk)
    row = get_object_or_404(MaterialUnit, pk=row_pk, material=material)
    label = str(row)
    row.delete()
    audit.record("material.conversion_delete", material, summary=f"{material}: removed {label}")
    messages.success(request, f"Removed {label}.")
    return redirect(_material_tab(material))


@permission_required("shop.manage_materials")
@require_POST
def barcode_add(request, pk):
    material = access.get_or_404(request.actor, PERMS["material"], Material.objects.all(), pk=pk)
    form = MaterialBarcodeForm(request.POST, material=material)
    if form.is_valid():
        row = form.save()
        audit.record("material.barcode", material, summary=f"{material}: barcode {row.code}")
        messages.success(request, f"Added barcode {row.code}.")
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect(_material_tab(material))


@permission_required("shop.manage_materials")
@require_POST
def barcode_delete(request, pk, row_pk):
    material = access.get_or_404(request.actor, PERMS["material"], Material.objects.all(), pk=pk)
    row = get_object_or_404(MaterialBarcode, pk=row_pk, material=material)
    code = row.code
    row.delete()
    audit.record("material.barcode_delete", material, summary=f"{material}: removed {code}")
    messages.success(request, f"Removed barcode {code}.")
    return redirect(_material_tab(material))


@login_required
def edit(request, kind, pk=None):
    if kind not in KINDS:
        raise Http404
    access.require(request.actor, PERMS[kind])
    model, form_class, back = KINDS[kind]
    if model is Material and pk is None and request.method == "GET":
        # While stock is on, a new material is added with its stock details.
        target = hooks.add_material_url()
        if target != request.path:
            query = request.GET.urlencode()
            return redirect(f"{target}?{query}" if query else target)
    obj = (
        access.get_or_404(request.actor, PERMS[kind], model.objects.all(), pk=pk) if pk else model()
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
        return redirect(obj if model is Material else back)
    noun = model._meta.verbose_name
    context = {
        "form": form,
        "title": f"Edit {obj}" if pk else f"Add {noun}",
        "back": back,
        "obj": obj,
        **(_archive_urls(request, kind, obj) if pk else {}),
    }
    if model is Material:
        context["back_url"] = obj.get_absolute_url() if pk else hooks.materials_home()
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


def _archive_urls(request, kind, obj) -> dict:
    return {
        "archive_url": reverse("shop:archive", args=[kind, obj.pk]),
        "delete_url": reverse("shop:delete", args=[kind, obj.pk])
        if access.has_perm(request.actor, PERMS[kind])
        else "",
    }


def _get(request, kind, pk, perm):
    if kind not in KINDS:
        raise Http404
    return access.get_or_404(request.actor, perm, KINDS[kind][0].objects.all(), pk=pk)


@login_required
@require_POST
def archive_record(request, kind, pk):
    """Archive or bring back a machine, material or vendor."""
    if kind not in KINDS:
        raise Http404
    access.require(request.actor, PERMS[kind])
    return archive.archive_view(request, _get(request, kind, pk, PERMS[kind]))


@login_required
def delete_record(request, kind, pk):
    if kind not in KINDS:
        raise Http404
    access.require(request.actor, PERMS[kind])
    obj = _get(request, kind, pk, PERMS[kind])
    done = hooks.materials_home() if kind == "material" else reverse(KINDS[kind][2])
    parts = {"material": "its units, barcodes and stock settings"}.get(kind, "")
    return archive.delete_view(
        request,
        obj,
        archive_url=reverse("shop:archive", args=[kind, pk]),
        done_url=done,
        parts=parts,
    )
