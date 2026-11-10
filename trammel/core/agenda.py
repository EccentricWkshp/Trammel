"""Entries other modules put on the calendar (FR-JOB-9).

The calendar shows jobs' dates; a module adds its own dated items, such as the
deliveries purchasing expects:

    @agenda.register(permission="purchasing.view")
    def deliveries(request, start, end): return [agenda.Entry(...), ...]

Providers of disabled modules, or that the person lacks the permission for, are
skipped.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

_providers: list[tuple[Callable, str | None]] = []


@dataclass
class Entry:
    day: dt.date
    #: What kind of date it is: “Due”, “Delivery”…
    label: str
    text: str
    url: str
    late: bool = False
    #: A CSS hook so kinds can look different (“job”, “delivery”).
    kind: str = ""


def register(*, permission: str | None = None):
    def decorator(fn: Callable) -> Callable:
        _providers.append((fn, permission))
        return fn

    return decorator


def _available(request) -> list[Callable]:
    from . import access, modules

    on = modules.enabled_keys()
    return [
        fn
        for fn, permission in _providers
        if ((key := modules.module_of(fn.__module__)) is None or key in on)
        and (not permission or access.has_perm(request.actor, permission))
    ]


def any_for(request) -> bool:
    """Whether anything besides jobs can appear on this person's calendar."""
    return bool(_available(request))


def entries(request, start: dt.date, end: dt.date) -> list[Entry]:
    out: list[Entry] = []
    for fn in _available(request):
        out += [e for e in fn(request, start, end) if start <= e.day <= end]
    return out
