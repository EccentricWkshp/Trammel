"""
Background tasks on PostgreSQL (SPEC §3.5) — no Redis, no broker.

    @task("files.thumbnail")
    def make_thumbnail(payload): ...

    enqueue("files.thumbnail", {"blob": str(blob.pk)})

    @periodic("quotes.expire", every=timedelta(hours=1))
    def expire_quotes(payload): ...

The worker (`manage.py run_worker`) claims due tasks with
`SELECT … FOR UPDATE SKIP LOCKED`, so several workers never run the same task,
and retries failures with exponential backoff. Periodic tasks are enqueued by
the worker when due; their last run is kept in `PeriodicRun`.

Tests and the `--once` flag call `run_pending()` to drain the queue in-process.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import audit
from .access import SYSTEM

log = logging.getLogger("trammel.tasks")

_handlers: dict[str, Callable[[dict], object]] = {}


@dataclass(frozen=True)
class Periodic:
    name: str
    every: timedelta


_periodic: dict[str, Periodic] = {}


def task(name: str):
    def decorator(fn):
        if name in _handlers and _handlers[name] is not fn:
            raise ValueError(f"Task {name!r} registered twice.")
        _handlers[name] = fn
        return fn

    return decorator


def periodic(name: str, every: timedelta):
    def decorator(fn):
        task(name)(fn)
        _periodic[name] = Periodic(name, every)
        return fn

    return decorator


def enqueue(name: str, payload: dict | None = None, *, run_after=None, dedupe: bool = False):
    """Queue a task. With `dedupe`, an identical pending task is not queued twice.

    Enqueued tasks become visible when the surrounding transaction commits, so a
    task never runs against rows that were rolled back.
    """
    from .models import BackgroundTask

    if name not in _handlers:
        raise LookupError(f"No task named {name!r}.")
    payload = payload or {}
    if (
        dedupe
        and BackgroundTask.objects.filter(
            name=name, payload=payload, state=BackgroundTask.State.PENDING
        ).exists()
    ):
        return None
    return BackgroundTask.objects.create(
        name=name, payload=payload, run_after=run_after or timezone.now()
    )


def _claim():
    from .models import BackgroundTask

    with transaction.atomic():
        row = (
            BackgroundTask.objects.select_for_update(skip_locked=True)
            .filter(state=BackgroundTask.State.PENDING, run_after__lte=timezone.now())
            .order_by("run_after")
            .first()
        )
        if row is None:
            return None
        row.state = BackgroundTask.State.RUNNING
        row.locked_at = timezone.now()
        row.attempts += 1
        row.save(update_fields=["state", "locked_at", "attempts"])
        return row


def _run(row) -> None:
    from .models import BackgroundTask

    handler = _handlers.get(row.name)
    try:
        if handler is None:
            raise LookupError(f"No handler for task {row.name!r}.")
        with audit.acting_as(SYSTEM, source="system"):
            handler(row.payload)
    except Exception as exc:  # noqa: BLE001 — every failure is recorded, none is fatal
        log.exception("Task %s failed", row.name)
        row.last_error = f"{exc}\n\n{traceback.format_exc()}"[-4000:]
        if row.attempts >= row.max_attempts:
            row.state = BackgroundTask.State.FAILED
            row.finished_at = timezone.now()
        else:
            row.state = BackgroundTask.State.PENDING
            row.run_after = timezone.now() + timedelta(seconds=min(3600, 10 * 2**row.attempts))
    else:
        row.state = BackgroundTask.State.DONE
        row.finished_at = timezone.now()
        row.last_error = ""
    row.save(update_fields=["state", "finished_at", "last_error", "run_after"])


def schedule_periodic() -> int:
    """Enqueue every periodic task that is due. Returns how many were queued."""
    from .models import BackgroundTask, PeriodicRun

    now = timezone.now()
    queued = 0
    for spec in _periodic.values():
        with transaction.atomic():
            PeriodicRun.objects.get_or_create(name=spec.name)
            run = PeriodicRun.objects.select_for_update().get(name=spec.name)
            if run.last_enqueued_at and now - run.last_enqueued_at < spec.every:
                continue
            busy = BackgroundTask.objects.filter(
                name=spec.name,
                state__in=[BackgroundTask.State.PENDING, BackgroundTask.State.RUNNING],
            ).exists()
            if not busy:
                enqueue(spec.name)
                queued += 1
            run.last_enqueued_at = now
            run.save(update_fields=["last_enqueued_at"])
    return queued


def reclaim_stale(minutes: int = 60) -> int:
    """Put tasks back whose worker died while running them."""
    from .models import BackgroundTask

    cutoff = timezone.now() - timedelta(minutes=minutes)
    return BackgroundTask.objects.filter(
        state=BackgroundTask.State.RUNNING, locked_at__lt=cutoff
    ).update(state=BackgroundTask.State.PENDING)


def run_pending(limit: int = 1000) -> int:
    """Run due tasks in this process until none are left. Returns how many ran."""
    ran = 0
    while ran < limit and (row := _claim()) is not None:
        _run(row)
        ran += 1
    return ran
