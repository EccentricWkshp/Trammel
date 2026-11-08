from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from trammel.core import audit, tasks
from trammel.core.services import save_audited
from trammel.core.web import paginate, permission_required

from . import importers, services
from .forms import (
    BoundaryForm,
    ImportForm,
    JurisdictionForm,
    LookupForm,
    TaxCategoryForm,
    TaxRateForm,
    TaxSettingsForm,
)
from .models import Boundary, ImportBatch, Jurisdiction, Source, TaxCategory, TaxRate, TaxSettings


def _form_page(request, form, title, back):
    return render(request, "core/settings_form.html", {"form": form, "title": title, "back": back})


@permission_required("tax.view")
def overview(request):
    q = request.GET.get("q", "").strip()
    jurisdictions = Jurisdiction.objects.prefetch_related("rates")
    if q:
        jurisdictions = jurisdictions.filter(code__icontains=q) | jurisdictions.filter(
            name__icontains=q
        )
    lookup = LookupForm(request.GET if "postal_code" in request.GET else None)
    result = None
    if lookup.is_bound and lookup.is_valid():
        on = lookup.cleaned_data["on"] or timezone.localdate()
        result = services.lookup_postal(lookup.cleaned_data["postal_code"], on)
    return render(
        request,
        "tax/overview.html",
        {
            "page": paginate(request, jurisdictions, 50),
            "q": q,
            "settings": TaxSettings.load(),
            "categories": TaxCategory.objects.all(),
            "batches": ImportBatch.objects.all()[:10],
            "lookup": lookup,
            "result": result,
            "query": f"q={q}" if q else "",
        },
    )


@permission_required("tax.manage")
def settings_edit(request):
    obj = TaxSettings.load()
    before = audit.snapshot(obj)
    form = TaxSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        save_audited(form.save(commit=False), before=before, summary="Edited tax settings")
        messages.success(request, "Tax settings saved.")
        return redirect("tax:overview")
    return _form_page(request, form, "Tax settings", "tax:overview")


@permission_required("tax.manage")
def jurisdiction_edit(request, pk=None):
    obj = get_object_or_404(Jurisdiction, pk=pk) if pk else Jurisdiction(source=Source.MANUAL)
    before = audit.snapshot(obj) if pk else None
    form = JurisdictionForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        save_audited(form.save(commit=False), before=before)
        messages.success(request, f"{obj} saved.")
        return redirect("tax:overview")
    return _form_page(request, form, f"Edit {obj}" if pk else "Add jurisdiction", "tax:overview")


@permission_required("tax.manage")
def rate_create(request):
    initial = {"jurisdiction": request.GET.get("jurisdiction")}
    form = TaxRateForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        save_audited(form.save(commit=False))
        messages.success(request, "Rate added. Earlier invoices keep the rate they used.")
        return redirect("tax:overview")
    return _form_page(request, form, "Add tax rate", "tax:overview")


@permission_required("tax.manage")
def rate_end(request, pk):
    """Rates are never edited once used; a rate is ended and a new one added."""
    rate = get_object_or_404(TaxRate, pk=pk)
    if request.method == "POST":
        before = audit.snapshot(rate)
        try:
            rate.effective_to = request.POST.get("effective_to") or timezone.localdate()
            save_audited(rate, before=before, summary=f"Ended rate {rate}")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect("tax:overview")
    return render(request, "tax/rate_end.html", {"rate": rate})


@permission_required("tax.manage")
def boundary_create(request):
    form = BoundaryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        jurisdiction = form.cleaned_data["jurisdiction"]
        created = 0
        try:
            with transaction.atomic():
                for line in form.cleaned_data["postal_codes"].splitlines():
                    if line.strip():
                        low, high = importers._zip_range(line)
                        boundary = Boundary.objects.create(
                            zip_low=low,
                            zip_high=high,
                            jurisdiction_codes=[jurisdiction.code],
                            location_code=jurisdiction.code,
                            source=Source.MANUAL,
                        )
                        audit.record_create(boundary)
                        created += 1
        except ValueError as exc:
            form.add_error("postal_codes", str(exc))
        else:
            messages.success(request, f"{created} postal code range(s) mapped to {jurisdiction}.")
            return redirect("tax:overview")
    return _form_page(request, form, "Map postal codes to a jurisdiction", "tax:overview")


@permission_required("tax.manage")
def category_edit(request, pk=None):
    obj = get_object_or_404(TaxCategory, pk=pk) if pk else TaxCategory()
    before = audit.snapshot(obj) if pk else None
    form = TaxCategoryForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            category = save_audited(form.save(commit=False), before=before)
            if category.is_default:
                TaxCategory.objects.exclude(pk=category.pk).update(is_default=False)
        messages.success(request, f"{obj} saved.")
        return redirect("tax:overview")
    return _form_page(request, form, f"Edit {obj}" if pk else "Add tax category", "tax:overview")


@permission_required("tax.manage")
def import_file(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        settings = TaxSettings.load()
        kind = form.cleaned_data["kind"]
        enabled = settings.csv_enabled if kind == "csv" else settings.sst_enabled
        if not enabled:
            form.add_error("kind", "That rate source is turned off in tax settings.")
        elif kind == "csv":
            try:
                batch = importers.import_generic_csv(
                    upload.read(), filename=upload.name, user=request.user
                )
            except ValidationError as exc:
                form.add_error("file", exc)
            else:
                messages.success(
                    request,
                    f"Imported {batch.rows} row(s)"
                    + (f"; skipped {batch.skipped}." if batch.skipped else "."),
                )
                return redirect("tax:overview")
        else:
            url = form.cleaned_data["url"]
            payload = {}
            try:
                if url:
                    importers.check_url(url)
                    payload["url"] = url
                else:
                    payload["path"] = str(_spool(upload))
            except ValidationError as exc:
                form.add_error("url" if url else "file", exc)
            else:
                name = url.rsplit("/", 1)[-1] if url else upload.name
                batch = ImportBatch.objects.create(
                    source=Source.SST,
                    kind="sst",
                    filename=name[:255],
                    imported_by=request.user,
                    status="queued",
                    source_url=url or "",
                )
                tasks.enqueue("tax.import_sst", {"batch": str(batch.pk), **payload})
                messages.success(
                    request,
                    f"Importing {name} in the background. A state's "
                    "boundary file can take a few minutes; the result appears "
                    "under Recent imports.",
                )
                return redirect("tax:overview")
    return render(request, "tax/import.html", {"form": form})


def _spool(upload):
    """Save an upload where the worker can read it."""
    import tempfile
    from pathlib import Path

    from django.conf import settings as django_settings

    tmp = Path(django_settings.MEDIA_ROOT) / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=tmp, suffix=Path(upload.name).suffix)
    with open(fd, "wb") as out:
        for chunk in upload.chunks():
            out.write(chunk)
    return Path(name)
