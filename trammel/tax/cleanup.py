"""Removing imported tax data: a whole import, or rates in bulk.

Invoices keep the rates they used as a snapshot (FR-TAX-7), so removing a rate
or a boundary never changes an issued invoice. A jurisdiction that was only
there to hold imported rates goes with its last rate, unless an invoice chose
it by hand (it is kept for that invoice). Jurisdictions entered by hand stay,
so a new rate can be added to them.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, QuerySet

from trammel.core import audit

from .models import Boundary, ImportBatch, Jurisdiction, Source, TaxRate


def prune_jurisdictions(jurisdictions: QuerySet[Jurisdiction]) -> int:
    """Delete the imported jurisdictions here that no rate and no invoice uses."""
    from trammel.billing.models import Invoice

    unused = (
        jurisdictions.exclude(source=Source.MANUAL)
        .exclude(Exists(TaxRate.objects.filter(jurisdiction=OuterRef("pk"))))
        .exclude(Exists(Invoice.objects.filter(manual_jurisdiction=OuterRef("pk"))))
    )
    deleted, _ = Jurisdiction.objects.filter(pk__in=unused.values("pk")).delete()
    return deleted


def mark_replaced(keep: ImportBatch | None = None) -> int:
    """Imports whose every rate and boundary has since been replaced read as such."""
    batches = (
        ImportBatch.objects.filter(status="done", rows__gt=0)
        .exclude(Exists(TaxRate.objects.filter(batch=OuterRef("pk"))))
        .exclude(Exists(Boundary.objects.filter(batch=OuterRef("pk"))))
    )
    if keep is not None:
        batches = batches.exclude(pk=keep.pk)
    return batches.update(status="replaced")


@transaction.atomic
def remove_batch(batch: ImportBatch, *, reason: str = "") -> dict:
    """Remove an import with everything it brought in. Returns what went."""
    if batch.status in ("queued", "running"):
        raise ValidationError("That import is still running; remove it once it has finished.")
    touched = Jurisdiction.objects.filter(rates__batch=batch).distinct()
    touched_pks = list(touched.values_list("pk", flat=True))
    rates, _ = TaxRate.objects.filter(batch=batch).delete()
    boundaries, _ = Boundary.objects.filter(batch=batch).delete()
    jurisdictions = prune_jurisdictions(Jurisdiction.objects.filter(pk__in=touched_pks))
    counts = {"rates": rates, "boundaries": boundaries, "jurisdictions": jurisdictions}
    audit.record(
        "tax.import_removed",
        batch,
        summary=f"Removed the import {batch}: {rates} rate(s), {boundaries} postal code "
        f"range(s), {jurisdictions} jurisdiction(s)",
        after=counts,
        reason=reason,
    )
    batch.delete()
    return counts


@transaction.atomic
def remove_rates(rates: QuerySet[TaxRate], *, reason: str = "") -> dict:
    """Remove these rates, and the imported jurisdictions left with none."""
    rates = TaxRate.objects.filter(pk__in=rates.values("pk"))
    touched_pks = list(rates.values_list("jurisdiction_id", flat=True).distinct())
    count, _ = rates.delete()
    jurisdictions = prune_jurisdictions(Jurisdiction.objects.filter(pk__in=touched_pks))
    mark_replaced()
    counts = {"rates": count, "jurisdictions": jurisdictions}
    audit.record(
        "tax.rates_removed",
        entity_type="tax.taxrate",
        entity_label=f"{count} rate(s)",
        summary=f"Removed {count} tax rate(s) and {jurisdictions} jurisdiction(s) left without one",
        after=counts,
        reason=reason,
    )
    return counts


def preview_batch(batch: ImportBatch) -> dict:
    """What removing an import would take with it."""
    return {
        "rates": TaxRate.objects.filter(batch=batch).count(),
        "boundaries": Boundary.objects.filter(batch=batch).count(),
    }
