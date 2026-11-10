from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.http import require_POST

from trammel.core import access, audit, templating
from trammel.core.services import save_audited
from trammel.core.web import (
    csv_response,
    document_response,
    document_view,
    paginate,
    permission_required,
)
from trammel.shop.models import Material, MaterialBarcode

from . import hooks, labels, services
from .floor import LENGTH_UNITS
from .forms import (
    BarcodeForm,
    CountForm,
    LocationForm,
    LotActionForm,
    ReceiveForm,
    SettingsForm,
    StockSectionForm,
    TrackingForm,
)
from .models import Location, Reason, StockLot, StockMove, StockSettings


def _errors(request, exc) -> None:
    if isinstance(exc, ValidationError):
        messages.error(request, " ".join(exc.messages))
    else:
        messages.error(request, " ".join(e for errs in exc.values() for e in errs))


def _stock_tab(material) -> str:
    return f"{material.get_absolute_url()}?tab=stock"


def _material(request, pk) -> Material:
    return access.get_or_404(request.actor, "shop.view", Material.objects.all(), pk=pk)


# ---------------------------------------------------------------------------
# Lists and reports
# ---------------------------------------------------------------------------


SHOW = (
    ("", "All materials"),
    ("tracked", "Tracked in stock"),
    ("low", "Low stock"),
    ("untracked", "Not tracked"),
)


def _filtered_materials(request):
    """Every material, with what is on hand for those tracked in stock."""
    qs = Material.objects.select_related("vendor").annotate(
        tracked=Coalesce(F("stock__tracked"), Value(False)),
        min_quantity=F("stock__min_quantity"),
        kept_in=F("stock__default_location__path"),
        on_hand=Coalesce(Sum("stock__lots__qty_on_hand"), Decimal("0")),
        value=Coalesce(Sum("stock__lots__value_on_hand"), Decimal("0")),
    )
    term = request.GET.get("q", "").strip()
    if term:
        matching = Material.objects.filter(
            Q(name__icontains=term)
            | Q(sku__icontains=term)
            | Q(vendor__name__icontains=term)
            | Q(barcodes__code=term)
        ).values("pk")
        qs = qs.filter(pk__in=matching)
    category = request.GET.get("category", "")
    if category in Material.Category.values:
        qs = qs.filter(category=category)
    if request.GET.get("inactive") != "1":
        qs = qs.filter(is_active=True)
    show = request.GET.get("show", "")
    if show == "tracked":
        qs = qs.filter(tracked=True)
    elif show == "untracked":
        qs = qs.filter(tracked=False)
    elif show == "low":
        qs = qs.filter(tracked=True, min_quantity__isnull=False, on_hand__lte=F("min_quantity"))
    return qs.order_by("name"), term, category, show


@permission_required("shop.view")
def stock_list(request):
    """The materials home: every material, and for those tracked, what is on hand."""
    qs, term, category, show = _filtered_materials(request)
    actor = request.actor
    can_stock = access.has_perm(actor, "stock.view")
    can_costs = access.has_perm(actor, "core.see_costs")
    total = None
    if can_costs and can_stock:
        total = StockLot.objects.filter(item__tracked=True).aggregate(v=Sum("value_on_hand"))["v"]
    page = paginate(request, qs)
    query = urlencode({k: v for k, v in request.GET.items() if k != "page" and v})
    return render(
        request,
        "stock/list.html",
        {
            "page": page,
            "q": term,
            "category": category,
            "categories": Material.Category.choices,
            "show": show,
            "shows": SHOW,
            "inactive": request.GET.get("inactive") == "1",
            "query": query,
            "can_stock": can_stock,
            "can_costs": can_costs,
            "total_value": total or Decimal("0"),
            "can_manage": access.has_perm(actor, "stock.manage"),
            "can_add_material": access.has_perm(actor, "shop.edit"),
        },
    )


@permission_required("stock.view")
def valuation_csv(request):
    if not access.has_perm(request.actor, "core.see_costs"):
        raise Http404
    audit.record("export", entity_type="stock.stocklot", summary="Exported stock valuation")
    lots = (
        StockLot.objects.filter(qty_on_hand__gt=0)
        .select_related("item__material", "location")
        .order_by("item__material__name", "received_on")
    )
    rows = (
        (
            x.item.material.name,
            x.item.material.sku,
            x.number,
            x.location.path,
            x.received_on,
            x.qty_on_hand,
            x.item.material.unit,
            x.unit_cost,
            x.value_on_hand,
        )
        for x in lots.iterator()
    )
    return csv_response(
        "stock-valuation.csv",
        ["material", "sku", "lot", "location", "received", "on_hand", "unit", "unit_cost", "value"],
        rows,
    )


@permission_required("stock.view")
def restock(request):
    rows = services.restock_list()
    if request.GET.get("format") == "csv":
        audit.record("export", entity_type="stock.stockitem", summary="Exported restock list")
        return csv_response(
            "restock.csv",
            ["material", "sku", "vendor", "on_hand", "restock_at", "reorder", "unit"],
            (
                (
                    i.material.name,
                    i.material.sku,
                    i.material.vendor.name if i.material.vendor else "",
                    i.on_hand,
                    i.min_quantity,
                    i.reorder_quantity,
                    i.material.unit,
                )
                for i in rows.select_related("material__vendor")
            ),
        )
    rows = list(rows.select_related("material__vendor"))
    ordered = hooks.ordered([i.pk for i in rows])
    for item in rows:
        item.on_order = ordered.get(item.pk)
    return render(
        request,
        "stock/restock.html",
        {
            "rows": rows,
            "show_on_order": hooks.tracks_orders(),
            "actions": hooks.restock_actions(request.actor),
        },
    )


def _moves(request):
    qs = StockMove.objects.select_related("item__material", "lot", "location", "user")
    term = request.GET.get("q", "").strip()
    if term:
        qs = qs.filter(
            Q(item__material__name__icontains=term)
            | Q(lot__number__icontains=term)
            | Q(note__icontains=term)
        )
    reason = request.GET.get("reason", "")
    if reason in Reason.values:
        qs = qs.filter(reason=reason)
    start, end = _date(request.GET.get("from")), _date(request.GET.get("to"))
    if start:
        qs = qs.filter(created_at__date__gte=start)
    if end:
        qs = qs.filter(created_at__date__lte=end)
    return qs.order_by("-created_at", "-id"), term, reason, start, end


def _date(value):
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


@permission_required("stock.view")
def moves(request):
    qs, term, reason, start, end = _moves(request)
    can_costs = access.has_perm(request.actor, "core.see_costs")
    if request.GET.get("format") == "csv":
        audit.record("export", entity_type="stock.stockmove", summary="Exported stock movements")
        header = ["date", "material", "lot", "location", "reason", "quantity", "unit", "note"]
        if can_costs:
            header.append("value")

        def rows():
            for m in qs.iterator():
                row = [
                    m.created_at.date(),
                    m.item.material.name,
                    m.lot.number,
                    m.location.path,
                    m.get_reason_display(),
                    m.quantity,
                    m.item.material.unit,
                    m.note,
                ]
                yield [*row, m.value] if can_costs else row

        return csv_response("stock-movements.csv", header, rows())
    page = paginate(request, qs)
    query = urlencode({k: v for k, v in request.GET.items() if k != "page" and v})
    return render(
        request,
        "stock/moves.html",
        {
            "page": page,
            "q": term,
            "reason": reason,
            "reasons": Reason.choices,
            "start": start,
            "end": end,
            "query": query,
            "can_costs": can_costs,
        },
    )


# ---------------------------------------------------------------------------
# Locations and lots
# ---------------------------------------------------------------------------


@permission_required("stock.view")
def location_list(request):
    locations = Location.objects.annotate(
        lot_count=Count("lots", filter=Q(lots__qty_on_hand__gt=0))
    )
    return render(
        request,
        "stock/locations.html",
        {
            "locations": locations,
            "can_manage": access.has_perm(request.actor, "stock.manage"),
        },
    )


@permission_required("stock.view")
def location_detail(request, pk):
    location = get_object_or_404(Location, pk=pk)
    lots = (
        StockLot.objects.filter(location=location, qty_on_hand__gt=0)
        .select_related("item__material")
        .order_by("item__material__name", "received_on")
    )
    return render(
        request,
        "stock/location.html",
        {
            "location": location,
            "lots": lots,
            "children": location.children.all(),
            "can_manage": access.has_perm(request.actor, "stock.manage"),
            "can_costs": access.has_perm(request.actor, "core.see_costs"),
        },
    )


@permission_required("stock.manage")
def location_edit(request, pk=None):
    location = get_object_or_404(Location, pk=pk) if pk else Location()
    before = audit.snapshot(location) if pk else None
    initial = {"parent": request.GET.get("parent")} if not pk else None
    form = LocationForm(request.POST or None, instance=location, initial=initial)
    if request.method == "POST":
        if "delete" in request.POST and pk:
            try:
                services.delete_location(location)
            except ValidationError as exc:
                _errors(request, exc)
                return redirect(location)
            messages.success(request, f"Deleted {location}.")
            return redirect("stock:locations")
        if form.is_valid():
            try:
                location = services.save_location(form.save(commit=False), before=before)
            except ValidationError as exc:
                for field, errs in exc.message_dict.items():
                    form.add_error(field if field in form.fields else None, errs)
            else:
                messages.success(request, f"{location} saved.")
                return redirect(location)
    return render(
        request,
        "stock/location_form.html",
        {
            "form": form,
            "location": location,
            "editing": bool(pk),
            "title": f"Edit {location}" if pk else "Add location",
        },
    )


@permission_required("stock.view")
def lot_detail(request, pk):
    lot = get_object_or_404(
        StockLot.objects.select_related("item__material", "location", "vendor", "parent"), pk=pk
    )
    material = lot.item.material
    can_edit = access.has_perm(request.actor, "stock.edit") and lot.item.tracked
    return render(
        request,
        "stock/lot.html",
        {
            "lot": lot,
            "material": material,
            "moves": lot.moves.select_related("location", "user").order_by("created_at", "id"),
            "can_edit": can_edit,
            "can_costs": access.has_perm(request.actor, "core.see_costs"),
            "locations": Location.objects.filter(is_active=True) if can_edit else [],
            "length_units": LENGTH_UNITS,
        },
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@permission_required("shop.edit")
def add_material(request):
    """Add a material and, in the same form, keep it in stock: where it is kept,
    when to restock it, and what is on hand. Replaces the plain material form
    while stock is enabled."""
    from trammel.shop.forms import MaterialForm

    can_stock = access.has_perm(request.actor, "stock.manage")
    can_receive = access.has_perm(request.actor, "stock.edit")
    initial = {"vendor": request.GET.get("vendor")} if request.GET.get("vendor") else None
    data = request.POST or None
    form = MaterialForm(data, instance=Material(), initial=initial)
    barcode_form = BarcodeForm(data, initial={"barcode": request.GET.get("barcode", "")})
    stock_form = (
        StockSectionForm(data, can_receive=can_receive, optional=True) if can_stock else None
    )
    if request.method == "POST":
        valid = form.is_valid()
        valid = barcode_form.is_valid() and valid
        valid = (stock_form is None or stock_form.is_valid()) and valid
        if valid:
            try:
                with transaction.atomic():
                    material = save_audited(form.save(commit=False))
                    if code := barcode_form.cleaned_data["barcode"]:
                        MaterialBarcode.objects.create(material=material, code=code)
                    keep = stock_form is not None and stock_form.wanted
                    if keep:
                        _start_tracking(request, material, stock_form.cleaned_data, can_receive)
            except ValidationError as exc:
                _form_errors(stock_form or form, exc)
            else:
                if keep:
                    messages.success(request, f"{material} added and kept in stock.")
                    return redirect(_stock_tab(material))
                messages.success(request, f"{material} added.")
                return redirect(material)
    return render(
        request,
        "stock/add_material.html",
        {
            "form": form,
            "barcode_form": barcode_form,
            "stock_form": stock_form,
            "has_locations": Location.objects.exists(),
            "title": "Add material",
        },
    )


@permission_required("stock.manage")
def track(request, pk):
    """Start tracking a material that is already on the list."""
    material = _material(request, pk)
    can_receive = access.has_perm(request.actor, "stock.edit")
    item = services.item_for(material)
    if item is not None and item.tracked:
        return redirect(_stock_tab(material))
    stock_form = StockSectionForm(request.POST or None, can_receive=can_receive, optional=False)
    if request.method == "POST" and stock_form.is_valid():
        try:
            with transaction.atomic():
                _start_tracking(request, material, stock_form.cleaned_data, can_receive)
        except ValidationError as exc:
            _form_errors(stock_form, exc)
        else:
            messages.success(request, f"{material} is now kept in stock.")
            return redirect(_stock_tab(material))
    return render(
        request,
        "stock/add_material.html",
        {
            "material": material,
            "stock_form": stock_form,
            "has_locations": Location.objects.exists(),
            "title": f"Keep {material} in stock",
        },
    )


def _form_errors(form, exc: ValidationError) -> None:
    for field, errs in getattr(exc, "message_dict", {None: exc.messages}).items():
        form.add_error(field if field in form.fields else None, errs)


def _start_tracking(request, material: Material, d: dict, can_receive: bool) -> None:
    location = d["location"]
    if location is None:
        location = services.save_location(Location(name=d["new_location"].strip()))
    services.set_tracking(
        material,
        tracked=True,
        min_quantity=d["min_quantity"],
        reorder_quantity=d["reorder_quantity"],
        default_location=location,
    )
    if can_receive and d.get("on_hand"):
        cost = d["on_hand_cost"]
        services.receive(
            material,
            quantity=d["on_hand"],
            location=location,
            unit_cost=material.unit_cost if cost is None else cost,
            vendor=material.vendor,
            opening=True,
            user=request.user,
        )


@permission_required("stock.manage")
@require_POST
def tracking(request, pk):
    material = _material(request, pk)
    item = services.item_for(material)
    form = TrackingForm(request.POST, instance=item, material=material)
    if form.is_valid():
        d = form.cleaned_data
        try:
            services.set_tracking(
                material,
                tracked=d["tracked"],
                min_quantity=d["min_quantity"],
                reorder_quantity=d["reorder_quantity"],
                default_location=d["default_location"],
            )
        except ValidationError as exc:
            _errors(request, exc)
        else:
            messages.success(request, f"Stock settings for {material} saved.")
    else:
        _errors(request, form.errors)
    return redirect(_stock_tab(material))


@permission_required("stock.edit")
@require_POST
def receive(request, pk):
    material = _material(request, pk)
    form = ReceiveForm(request.POST, material=material, item=services.item_for(material))
    if form.is_valid():
        d = form.cleaned_data
        try:
            lot = services.receive(
                material,
                quantity=d["quantity"],
                unit=d["unit"],
                location=d["location"],
                unit_cost=d["unit_cost"],
                total_cost=d["total_cost"],
                received_on=d["received_on"],
                vendor=d["vendor"],
                note=d["note"],
                opening=d["kind"] == "opening",
                update_cost=d["update_cost"],
                user=request.user,
            )
        except ValidationError as exc:
            _errors(request, exc)
        else:
            messages.success(request, f"{lot.number}: {material} is in {lot.location}.")
    else:
        _errors(request, form.errors)
    return redirect(_stock_tab(material))


@permission_required("stock.edit")
@require_POST
def count(request, pk):
    material = _material(request, pk)
    form = CountForm(request.POST, material=material, item=services.item_for(material))
    if form.is_valid():
        d = form.cleaned_data
        try:
            diff = services.count(
                material,
                location=d["location"],
                counted=d["counted"],
                unit=d["unit"],
                expected=d["expected"],
                note=d["note"],
                user=request.user,
            )
        except ValidationError as exc:
            _errors(request, exc)
        else:
            if diff:
                messages.success(
                    request,
                    f"Counted. {services.fmt(abs(diff), material.unit)} "
                    f"{'more' if diff > 0 else 'less'} than the records showed.",
                )
            else:
                messages.success(request, "Counted. It matches the records.")
    else:
        _errors(request, form.errors)
    return redirect(_next(request, _stock_tab(material)))


def _next(request, default: str) -> str:
    target = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return default


@permission_required("stock.edit")
@require_POST
def lot_action(request, pk, action):
    lot = get_object_or_404(StockLot.objects.select_related("item__material"), pk=pk)
    material = lot.item.material
    form = LotActionForm(request.POST, material=material)
    back = _next(request, _stock_tab(material))
    if not form.is_valid():
        _errors(request, form.errors)
        return redirect(back)
    d = form.cleaned_data
    try:
        if action == "transfer":
            if d["to"] is None:
                raise ValidationError("Choose where it goes.")
            moved = services.transfer(
                lot,
                quantity=d["quantity"],
                unit=d["unit"],
                to=d["to"],
                user=request.user,
                note=d["note"],
            )
            messages.success(request, f"Moved to {moved.location} ({moved.number}).")
        elif action == "scrap":
            services.scrap(
                lot, quantity=d["quantity"], unit=d["unit"], note=d["note"], user=request.user
            )
            messages.success(request, f"Scrapped from {lot.number}.")
        else:
            raise Http404
    except ValidationError as exc:
        _errors(request, exc)
    return redirect(back)


@permission_required("stock.edit")
@require_POST
def cut_remnant(request, pk):
    """Keep a piece of a lot as a remnant of its own (FR-STK-5)."""
    lot = get_object_or_404(StockLot.objects.select_related("item__material"), pk=pk)
    back = _next(request, _stock_tab(lot.item.material))
    to = Location.objects.filter(pk=request.POST.get("location") or None).first()
    try:
        remnant = services.cut_remnant(
            lot,
            length=request.POST.get("length", "").strip(),
            width=request.POST.get("width", "").strip() or None,
            unit=request.POST.get("unit", "in"),
            location=to,
            note=request.POST.get("note", "").strip(),
            user=request.user,
        )
    except ValidationError as exc:
        _errors(request, exc)
    else:
        messages.success(
            request, f"Remnant {remnant.number} ({remnant.size}) is in {remnant.location}."
        )
    return redirect(back)


@permission_required("stock.view")
def remnant_list(request):
    """Remnants on hand, found by the size needed, either way round (FR-STK-5)."""
    material = Material.objects.filter(pk=request.GET.get("material") or None).first()
    unit = request.GET.get("unit") if request.GET.get("unit") in services.AREA_OF else "in"
    length, width = request.GET.get("length", "").strip(), request.GET.get("width", "").strip()
    try:
        found = services.remnants(material, length=length or None, width=width or None, unit=unit)
    except ValidationError as exc:
        _errors(request, exc)
        found = services.remnants(material)
    if request.GET.get("format") == "csv":
        audit.record("export", entity_type="stock.stocklot", summary="Exported remnants")
        costs = access.has_perm(request.actor, "core.see_costs")
        header = ["material", "size", "lot", "location", "received", "cut"]
        header += ["value"] if costs else []
        return csv_response(
            "remnants.csv",
            header,
            (
                [
                    r.item.material.name,
                    r.size,
                    r.number,
                    r.location.path,
                    r.received_on,
                    r.created_at.date(),
                ]
                + ([r.value_on_hand] if costs else [])
                for r in found
            ),
        )
    return render(
        request,
        "stock/remnants.html",
        {
            "remnants": found[:200],
            "materials": Material.objects.filter(
                stock__lots__length__isnull=False, stock__lots__qty_on_hand__gt=0
            )
            .distinct()
            .order_by("name"),
            "material": material,
            "length": length,
            "width": width,
            "unit": unit,
            "units": [u for u in services.AREA_OF if u != "linear_ft"],
            "can_costs": access.has_perm(request.actor, "core.see_costs"),
        },
    )


@permission_required("stock.manage")
def settings_view(request):
    obj = StockSettings.load()
    before = audit.snapshot(obj)
    form = SettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit.record_update(obj, before)
        messages.success(request, "Stock settings saved.")
        return redirect("stock:settings")
    return render(request, "stock/settings.html", {"form": form})


# ---------------------------------------------------------------------------
# Labels (FR-STK-7)
# ---------------------------------------------------------------------------

SELECTION = ("lot", "location", "material", "lots_of", "lots_in", "all_locations")


def _uuids(request, name: str) -> list[uuid.UUID]:
    out = []
    for value in request.GET.getlist(name):
        try:
            out.append(uuid.UUID(value))
        except ValueError:
            continue
    return out


def _labels_for(request) -> list:
    lots = StockLot.objects.filter(
        Q(pk__in=_uuids(request, "lot"))
        | Q(item__material_id__in=_uuids(request, "lots_of"), qty_on_hand__gt=0)
        | Q(location_id__in=_uuids(request, "lots_in"), qty_on_hand__gt=0)
    ).select_related("item__material", "location")
    locations = Location.objects.filter(pk__in=_uuids(request, "location"))
    if request.GET.get("all_locations") == "1":
        locations = Location.objects.filter(is_active=True)
    materials = Material.objects.filter(pk__in=_uuids(request, "material"))
    return [
        *labels.for_locations(locations.select_related("parent").order_by("path")),
        *labels.for_materials(materials.order_by("name")),
        *labels.for_lots(lots.order_by("item__material__name", "received_on", "number")),
    ]


@permission_required("stock.view")
def label_options(request):
    """Choose the layout, where on the sheet to start, and how many of each."""
    found = _labels_for(request)
    if not found:
        messages.error(request, "Nothing to print labels for.")
        return redirect("stock:list")
    selection = [(k, v) for k in SELECTION for v in request.GET.getlist(k)]
    settings = StockSettings.load()
    return render(
        request,
        "stock/label_options.html",
        {
            "labels": found,
            "selection": selection,
            "positions": range(1, labels.PER_SHEET + 1),
            "settings": settings,
            "pdf": templating.pdf_available(),
        },
    )


@permission_required("stock.view")
@document_view
def label_print(request):
    found = _labels_for(request)
    if not found:
        raise Http404
    layout = "single" if request.GET.get("layout") == "single" else "sheet"
    copies = _int(request.GET.get("copies"), 1)
    settings = StockSettings.load()
    context = {"layout": layout}
    if layout == "sheet":
        context["pages"] = labels.sheets(
            found, start=_int(request.GET.get("start"), 1), copies=copies
        )
        context["qr_size"] = Decimal("0.86")
    else:
        context["singles"] = labels.singles(found, copies=copies)
        context["width"], context["height"] = settings.label_width, settings.label_height
        # Square, as tall as the label allows, but leaving most of the width for text.
        context["qr_size"] = max(
            Decimal("0.4"),
            min(settings.label_height - Decimal("0.14"), settings.label_width * Decimal("0.4")),
        )
    html = render_to_string("stock/labels.html", context, request=request)
    if request.GET.get("format") == "pdf":
        try:
            data = templating.render_pdf(html)
        except templating.PdfUnavailable as exc:
            return HttpResponse(str(exc), status=503, content_type="text/plain")
        response = HttpResponse(data, content_type="application/pdf")
        response["Content-Disposition"] = 'attachment; filename="labels.pdf"'
        return response
    return document_response(request, html)


def _int(value, default: int) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return default


# ---------------------------------------------------------------------------
# Reports (listed on the Reports page)
# ---------------------------------------------------------------------------

VALUATION_GROUPS = (("material", "Material"), ("category", "Category"), ("location", "Location"))


def _report_period(request):

    from django.utils import timezone

    today = timezone.localdate()
    start = _date(request.GET.get("start")) or today.replace(day=1)
    end = _date(request.GET.get("end")) or today
    return (start, end) if start <= end else (end, start)


@permission_required("stock.view")
def valuation_report(request):
    from django.utils import timezone

    as_of = _date(request.GET.get("as_of")) or timezone.localdate()
    group = request.GET.get("group", "material")
    if group not in dict(VALUATION_GROUPS):
        group = "material"
    rows, totals = services.valuation(as_of, group)
    costs = access.has_perm(request.actor, "core.see_costs")
    if request.GET.get("format") == "csv":
        audit.record("export", entity_type="stock.stockmove", summary="Exported stock valuation")
        header = [group] + (["quantity", "unit"] if group == "material" else [])
        header += ["value"] if costs else []
        return csv_response(
            f"stock-valuation-{as_of.isoformat()}.csv",
            header,
            (
                [r["key"][0]]
                + ([r["quantity"], r["key"][1]] if group == "material" else [])
                + ([r["value"]] if costs else [])
                for r in rows
            ),
        )
    return render(
        request,
        "stock/report_valuation.html",
        {
            "rows": rows,
            "totals": totals,
            "as_of": as_of,
            "group": group,
            "groups": VALUATION_GROUPS,
            "costs": costs,
        },
    )


@permission_required("stock.view")
def adjustments_report(request):
    start, end = _report_period(request)
    group = "material" if request.GET.get("group") == "material" else "reason"
    summary, detail, totals = services.adjustments(start, end, group)
    costs = access.has_perm(request.actor, "core.see_costs")
    if request.GET.get("format") == "csv":
        audit.record("export", entity_type="stock.stockmove", summary="Exported stock adjustments")
        header = ["date", "material", "lot", "location", "reason", "quantity", "unit", "note"]
        header += ["value"] if costs else []

        def lines():
            for m in detail:
                row = [
                    m.created_at.date(),
                    m.item.material.name,
                    m.lot.number,
                    m.location.path,
                    m.get_reason_display(),
                    m.quantity,
                    m.item.material.unit,
                    m.note,
                ]
                yield row + ([m.value] if costs else [])

        return csv_response("stock-adjustments.csv", header, lines())
    return render(
        request,
        "stock/report_adjustments.html",
        {
            "summary": summary,
            "detail": detail,
            "totals": totals,
            "start": start,
            "end": end,
            "group": group,
            "groups": (("reason", "Reason"), ("material", "Material")),
            "costs": costs,
        },
    )


# ---------------------------------------------------------------------------
# Bills of materials (FR-STK-6), edited on the product page
# ---------------------------------------------------------------------------


@permission_required("catalog.edit")
@require_POST
def bom_add(request, product_pk):
    from trammel.catalog.models import Product

    from .models import BOMLine

    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=product_pk)
    try:
        services.save_bom_line(_bom_fields(request, BOMLine(product=product)))
    except ValidationError as exc:
        _errors(request, exc)
    return redirect(f"{product.get_absolute_url()}?tab=bom")


def _bom_fields(request, line):
    """Fill a bill of materials line from the posted form."""
    material = Material.objects.filter(pk=request.POST.get("material") or None).first()
    if material is None:
        raise ValidationError("Choose a material.")
    try:
        quantity = Decimal(request.POST.get("quantity", "").strip())
    except ArithmeticError:
        raise ValidationError("Enter how much goes into one.") from None
    line.material = material
    line.quantity = quantity
    line.unit = request.POST.get("unit") or material.unit
    line.note = request.POST.get("note", "").strip()[:200]
    return line


@permission_required("catalog.edit")
@require_POST
def bom_edit(request, pk):
    from .models import BOMLine

    line = get_object_or_404(BOMLine.objects.select_related("product"), pk=pk)
    url = f"{line.product.get_absolute_url()}?tab=bom"
    try:
        services.save_bom_line(_bom_fields(request, line))
    except ValidationError as exc:
        _errors(request, exc)
        return redirect(f"{url}&edit={line.pk}")
    return redirect(url)


@permission_required("catalog.edit")
@require_POST
def bom_delete(request, pk):
    from .models import BOMLine

    line = get_object_or_404(BOMLine.objects.select_related("product"), pk=pk)
    product = line.product
    services.delete_bom_line(line)
    return redirect(f"{product.get_absolute_url()}?tab=bom")
