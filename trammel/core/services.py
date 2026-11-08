"""Write helpers shared by module service layers: validate, save, audit — in one transaction."""

from __future__ import annotations

from django.db import models, transaction

from . import audit


@transaction.atomic
def save_audited(
    obj: models.Model, *, before: dict | None = None, summary: str = ""
) -> models.Model:
    """Validate and save `obj`, recording a create or an update.

    `before` is `audit.snapshot(obj)` taken before the edit; ignored on create.
    """
    creating = obj._state.adding
    if creating and hasattr(obj, "created_by_id") and obj.created_by_id is None:
        actor = audit.current_actor()
        if actor.user is not None:
            obj.created_by = actor.user
    obj.full_clean()
    obj.save()
    if creating:
        audit.record_create(obj, summary=summary)
    else:
        audit.record_update(obj, before or {}, summary=summary)
    return obj


@transaction.atomic
def delete_audited(obj: models.Model, *, reason: str = "") -> None:
    """Soft-delete (for BaseModel records) and record it."""
    obj.delete()
    audit.record_delete(obj, reason=reason)
