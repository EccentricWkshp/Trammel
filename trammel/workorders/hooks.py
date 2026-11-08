"""Checks other modules add to work order transitions (time tracking: no running timers)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

_checks: dict[str, list[Callable]] = defaultdict(list)


def check(target: str):
    def decorator(fn):
        _checks[target].append(fn)
        return fn

    return decorator


def problems(work_order, target: str) -> list[str]:
    return [p for fn in _checks[target] for p in fn(work_order)]


def guard_for(target: str):
    def guard(work_order):
        return problems(work_order, target)

    return guard
