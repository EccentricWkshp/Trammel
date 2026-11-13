from __future__ import annotations

import uuid
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from trammel.core import audit, tasks
from trammel.core.services import save_audited
from trammel.core.web import paginate, permission_required

from . import cleanup, importers, services
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


def _filters(params) -> tuple[str, str]:
    q = params.get("q", "").strip()
    source = params.get("source", "")
    return q, source if source in Source.values else ""


def _jurisdictions(q: str, source: str):
    rows = Jurisdiction.objects.all()
    if q:
        rows = rows.filter(Q(code__icontains=q) | Q(name__icontains=q))
    if source:
        rows = rows.filter(Q(source=source) | Q(rates__source=source)).distinct()
    return rows


def _matching_rates(q: str, source: str):
    """The rates a search shows, for removing them all at once."""
    rates = TaxRate.objects.filter(jurisdiction__in=_jurisdictions(q, source).values("pk"))
    return rates.filter(source=source) if source else rates


@permission_required("tax.view")
def overview(request):
    q, source = _filters(request.GET)
    jurisdictions = _jurisdictions(q, source).prefetch_related("rates")
    query = urlencode({k: v for k, v in (("q", q), ("source", source)) if v})
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
            "source": source,
            "sources": Source.choices,
            "matching": _matching_rates(q, source).count() if query else 0,
            "no_boundaries": services.sst_rates_without_boundaries(),
            "settings": TaxSettings.load(),
            "categories": TaxCategory.objects.all(),
            "batches": ImportBatch.objects.all()[:10],
            "lookup": lookup,
            "result": result,
            "query": query,
        },
    )


@permission_required("tax.manage")
def import_remove(request, pk):
    """Remove an import and everything it brought in, after a confirmation."""
    batch = get_object_or_404(ImportBatch, pk=pk)
    if request.method == "POST":
        try:
            counts = cleanup.remove_batch(batch, reason=request.POST.get("reason", "").strip())
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(
                request,
                f"Removed {batch.filename}: {counts['rates']} rate(s), "
                f"{counts['boundaries']} postal code range(s).",
            )
        return redirect("tax:overview")
    preview = cleanup.preview_batch(batch)
    return render(
        request,
        "tax/confirm_remove.html",
        {
            "title": f"Remove the import {batch.filename}?",
            "lines": [
                f"{preview['rates']} rate(s)",
                f"{preview['boundaries']} postal code range(s)",
                "the imported jurisdictions left with no rate",
            ],
            "busy": batch.status in ("queued", "running"),
        },
    )


@permission_required("tax.manage")
def rates_remove(request):
    """Remove the ticked rates, or every rate a search shows, after a confirmation."""
    if request.method != "POST":
        return redirect("tax:overview")
    q, source = _filters(request.POST)
    if request.POST.get("matching"):
        rates = _matching_rates(q, source)
        hidden = [("matching", "1"), ("q", q), ("source", source)]
    else:
        ids = []
        for value in request.POST.getlist("rate"):
            try:
                ids.append(uuid.UUID(value))
            except ValueError:
                continue
        rates = TaxRate.objects.filter(pk__in=ids)
        hidden = [("rate", str(pk)) for pk in ids]
    count = rates.count()
    if not count:
        messages.error(request, "Tick the rates to remove first.")
        return redirect("tax:overview")
    if request.POST.get("confirm"):
        counts = cleanup.remove_rates(rates, reason=request.POST.get("reason", "").strip())
        messages.success(
            request,
            f"Removed {counts['rates']} rate(s)"
            + (
                f" and {counts['jurisdictions']} jurisdiction(s) left without one."
                if counts["jurisdictions"]
                else "."
            ),
        )
        return redirect("tax:overview")
    imported = rates.exclude(source=Source.MANUAL).count()
    return render(
        request,
        "tax/confirm_remove.html",
        {
            "title": f"Remove {count} rate(s)?",
            "lines": [
                f"{count - imported} entered by hand" if count - imported else "",
                f"{imported} imported" if imported else "",
            ],
            "hidden": hidden,
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
                    upload.read(),
                    filename=upload.name,
                    user=request.user,
                    replace=not form.cleaned_data["add_to_earlier"],
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
