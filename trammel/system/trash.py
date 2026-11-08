"""The 30-day trash (FR-ADM-9, SPEC §4.4)."""

from __future__ import annotations

from datetime import timedelta

from django.apps import apps
from django.db.models import ProtectedError
from django.utils import timezone

from trammel.core import audit
from trammel.core.models import TRASH_RETENTION_DAYS, BaseModel


def trashable_models():
    return [
        m
        for m in apps.get_models()
        if issubclass(m, BaseModel) and not m._meta.abstract and m.__module__.startswith("trammel.")
    ]


def items(limit_per_model: int = 200):
    """Soft-deleted records still restorable, newest first."""
    out = []
    for model in trashable_models():
        for obj in model.all_objects.in_trash().order_by("-deleted_at")[:limit_per_model]:
            out.append(
                {
                    "obj": obj,
                    "label": model._meta.label_lower,
                    "kind": model._meta.verbose_name,
                    "deleted_at": obj.deleted_at,
                    "expires": obj.deleted_at + timedelta(days=TRASH_RETENTION_DAYS),
                }
            )
    return sorted(out, key=lambda r: r["deleted_at"], reverse=True)


def restore(label: str, pk) -> object:
    model = apps.get_model(label)
    if model not in trashable_models():
        raise LookupError(label)
    obj = model.all_objects.in_trash().get(pk=pk)
    obj.restore()
    audit.record_restore(obj)
    return obj


def purge() -> int:
    """Hard-delete what has been in the trash longer than the retention period.

    A record still referenced by a kept one (a customer with invoices) cannot be
    purged and stays in the trash rather than breaking the reference.
    """
    cutoff = timezone.now() - timedelta(days=TRASH_RETENTION_DAYS)
    purged = 0
    for model in trashable_models():
        for obj in model.all_objects.filter(deleted_at__lt=cutoff):
            try:
                obj.delete(hard=True)
                purged += 1
            except ProtectedError:
                continue
    return purged
