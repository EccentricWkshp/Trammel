"""
Extension points other modules use to take part in the job lifecycle.

Jobs knows nothing about quotes, agreements, work orders or invoices; those
modules register here instead (SPEC §4.5 — no module imports a module that
depends on it):

* `check(target)` — a function returning problems that block moving a job to
  `target`. The agreements module blocks `in_progress` until the agreement is
  accepted (FR-AGR-4); work orders and invoices block `closed` (FR-JOB-4).
* `outstanding` — things a forced closeout must list and resolve: open work
  orders, running timers, unpaid balances (§6.2).
* `on_duplicate` — copy a module's part of a job when it is duplicated
  (FR-JOB-5: scope and quote lines; never files, time or money).
* `on_updated` — a job's details were edited: `fn(job, before)`, with the
  snapshot from before. Quotes add or remove a rush fee when the priority
  moves to or from Rush.
* `notices` — something on a job waiting for staff, flagged on its page and
  in the job list with a link to deal with it: the portal flags files the
  customer sent that are waiting for review.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

_checks: dict[str, list[Callable]] = defaultdict(list)
_outstanding: list[Callable] = []
_on_duplicate: list[Callable] = []
_on_updated: list[Callable] = []
_notices: list[Callable] = []


def check(target: str):
    """Register `fn(job) -> Iterable[str]` as a precondition for entering `target`."""

    def decorator(fn: Callable[..., Iterable[str]]):
        _checks[target].append(fn)
        return fn

    return decorator


def problems(job, target: str) -> list[str]:
    return [p for fn in _checks[target] for p in fn(job)]


def guard_for(target: str) -> Callable:
    """A lifecycle guard that asks every registered check for `target`."""

    def guard(job):
        return problems(job, target)

    guard.__name__ = f"checks_for_{target}"
    return guard


@dataclass
class Outstanding:
    """One unresolved item a forced closeout must deal with (§6.2)."""

    key: str  # unique per job, e.g. "invoice:<uuid>"
    label: str  # shown to the admin
    #: (value, label) choices; the first is the default.
    choices: list[tuple[str, str]]
    #: Called with the chosen value inside the closeout transaction.
    resolve: Callable[[str], None] = field(repr=False, default=lambda choice: None)
    detail: dict = field(default_factory=dict)


def register_outstanding(fn: Callable[..., Iterable[Outstanding]]):
    """Register `fn(job) -> Iterable[Outstanding]`."""
    _outstanding.append(fn)
    return fn


def outstanding(job) -> list[Outstanding]:
    return [item for fn in _outstanding for item in fn(job)]


def register_duplicate(fn: Callable):
    """Register `fn(source_job, new_job)`, called inside the duplicating transaction."""
    _on_duplicate.append(fn)
    return fn


def run_duplicate(source, new) -> None:
    for fn in _on_duplicate:
        fn(source, new)


@dataclass
class Notice:
    """Something on a job waiting for staff, with where to deal with it."""

    text: str
    url: str
    #: The flag's look: "risk" (amber) or "late" (red).
    level: str = "risk"


def register_notices(fn: Callable) -> Callable:
    """Register `fn(actor, jobs) -> {job_id: [Notice]}`, called once for a page
    of jobs; it checks the actor's permissions itself."""
    _notices.append(fn)
    return fn


def notices(actor, jobs) -> dict:
    """Each job's notices, by its id: {job_id: [Notice]}."""
    jobs = list(jobs)
    found: dict = defaultdict(list)
    if not jobs:
        return found
    for fn in _notices:
        for job_id, items in fn(actor, jobs).items():
            found[job_id].extend(items)
    return found


def on_updated(fn: Callable) -> Callable:
    """Register `fn(job, before)`, called after a job's details are saved."""
    _on_updated.append(fn)
    return fn


def run_updated(job, before: dict) -> None:
    for fn in _on_updated:
        fn(job, before)
