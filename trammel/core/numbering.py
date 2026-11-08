"""
Human-readable document numbers (SPEC §4.4): `J-2026-0042`, `INV-2026-0007`.

Numbers are allocated by locking the series' counter row for the rest of the
caller's transaction. If that transaction rolls back, so does the increment —
which is what makes the series gap-free. Allocation outside a transaction would
commit the increment on its own and could leave a gap, so it is refused.

Allocate at the moment a number becomes real: when a job is created, or when an
invoice is *issued* (a draft has no number), never speculatively.
"""

from __future__ import annotations

import datetime as dt

from django.db import connection
from django.utils import timezone

from .models import DocumentSequence, SequenceCounter


class NumberingError(RuntimeError):
    pass


def next_number(key: str, on: dt.date | None = None) -> str:
    if not connection.in_atomic_block:
        raise NumberingError(
            "Document numbers must be allocated inside the transaction that saves "
            "the document, or a rollback leaves a gap."
        )
    sequence = DocumentSequence.objects.get(key=key)
    year = (on or timezone.localdate()).year if sequence.yearly else 0
    # Create the period's counter if this is its first number. ON CONFLICT DO
    # NOTHING keeps two first-of-the-year allocations from colliding.
    SequenceCounter.objects.bulk_create(
        [SequenceCounter(sequence=sequence, year=year, last_value=0)], ignore_conflicts=True
    )
    counter = SequenceCounter.objects.select_for_update().get(sequence=sequence, year=year)
    counter.last_value += 1
    counter.save(update_fields=["last_value"])
    return sequence.format(counter.last_value, year)


def preview_next(key: str, on: dt.date | None = None) -> str:
    """What the next number would be, without allocating it — for settings screens."""
    sequence = DocumentSequence.objects.get(key=key)
    year = (on or timezone.localdate()).year if sequence.yearly else 0
    last = (
        SequenceCounter.objects.filter(sequence=sequence, year=year)
        .values_list("last_value", flat=True)
        .first()
        or 0
    )
    return sequence.format(last + 1, year)
