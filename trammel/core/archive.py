"""Archiving and deleting records, the same way everywhere.

* **Archive** (`is_active = False`): the record stays wherever it is already
  used, isn't offered for new ones, and can be brought back.
* **Delete**: only while nothing uses the record. It goes to the trash, where
  it can be restored for 30 days.

"In use" is found from the relations other modules declare, so a module never
needs to know who points at its records: any reverse foreign key with rows
counts, except a record's own parts (relations that cascade, such as a
product's bill of materials), which are checked in turn for anything using
them.

A module that keeps a part another way registers a cleanup with `on_delete`,
run just before the record goes to the trash (stock drops an unused stock
record for a material).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.shortcuts import redirect, render

from . import audit
from .services import save_audited

_cleanups: dict[str, list[Callable]] = defaultdict(list)


def on_delete(model_label: str):
    """Register `fn(obj)` to run as a record of that model goes to the trash."""

    def decorator(fn: Callable):
        _cleanups[model_label.lower()].append(fn)
        return fn

    return decorator


def _reverse_relations(model):
    for rel in model._meta.get_fields(include_hidden=True):
        # Reverse foreign keys and one-to-ones, including those named "+".
        if rel.auto_created and not rel.concrete and (rel.one_to_many or rel.one_to_one):
            yield rel


def in_use(obj, *, _depth: int = 0) -> bool:
    """Whether anything kept points at `obj` (or at one of its parts)."""
    for rel in _reverse_relations(type(obj)):
        related = rel.related_model._base_manager.filter(**{rel.field.name: obj})
        if rel.on_delete is models.CASCADE:
            if _depth < 3 and any(in_use(part, _depth=_depth + 1) for part in related):
                return True
        elif related.exists():
            return True
    return False


@transaction.atomic
def set_archived(obj, archived: bool):
    before = audit.snapshot(obj)
    obj.is_active = not archived
    return save_audited(obj, before=before)


@transaction.atomic
def delete_unused(obj, *, reason: str = "", refuse: str = "") -> None:
    """Move `obj` to the trash, or refuse with a ValidationError if anything
    uses it. `refuse` is an extra reason the caller has found (the default
    quote template, say)."""
    if refuse:
        raise ValidationError(refuse)
    noun = obj._meta.verbose_name
    if in_use(obj):
        raise ValidationError(
            f"This {noun} is in use, so it can't be deleted. Archive it instead: it stays "
            "where it is used but isn't offered for anything new."
        )
    for fn in _cleanups[obj._meta.label_lower]:
        fn(obj)
    obj.delete()
    audit.record_delete(obj, reason=reason)


# ---------------------------------------------------------------------------
# Views: each module's archive and delete URLs call these with the record it
# has already looked up and checked permission for.
# ---------------------------------------------------------------------------


def archive_view(request, obj, *, back: str | None = None, refuse: str = ""):
    """POST `archived=1` to archive, `archived=0` to bring back."""
    archived = request.POST.get("archived") == "1"
    if archived and refuse:
        messages.error(request, refuse)
    else:
        set_archived(obj, archived)
        if archived:
            messages.success(request, f"{obj} archived. It isn't offered for anything new.")
        else:
            messages.success(request, f"{obj} is back in use.")
    return redirect(back or obj.get_absolute_url())


def delete_view(
    request,
    obj,
    *,
    archive_url: str,
    done_url: str,
    back: str | None = None,
    parts: str = "",
    refuse: str = "",
):
    """Confirm, then delete an unused record; explain and offer archiving if it is used.

    `parts` names what goes to the trash with it ("its bill of materials").
    """
    back = back or obj.get_absolute_url()
    used = bool(refuse) or in_use(obj)
    if request.method == "POST" and not used:
        try:
            delete_unused(obj, reason=request.POST.get("reason", ""))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return redirect(back)
        messages.success(request, f"{obj} moved to the trash. It can be restored for 30 days.")
        return redirect(done_url)
    return render(
        request,
        "core/confirm_delete.html",
        {
            "obj": obj,
            "noun": obj._meta.verbose_name,
            "used": used,
            "refuse": refuse,
            "archive_url": archive_url,
            "back": back,
            "parts": parts,
        },
    )
