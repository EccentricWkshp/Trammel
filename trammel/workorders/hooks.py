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


# Material use. The stock module takes over recording for the materials it
# tracks: it draws the quantity from lots and writes the usages at lot cost.

_material_handlers: list[Callable] = []


def material_handler(fn):
    """Register `fn(wo, *, material, quantity, unit, ...)` returning the usages it
    wrote, or None to leave the material to the price list."""
    _material_handlers.append(fn)
    return fn


def _enabled(fns: list[Callable]) -> list[Callable]:
    from trammel.core import modules

    on = modules.enabled_keys()
    return [f for f in fns if (key := modules.module_of(f.__module__)) is None or key in on]


def lots_offered() -> bool:
    """Whether a stock module is drawing materials from lots, so a lot can be chosen."""
    return bool(_enabled(_material_handlers))


def handle_material(work_order, **kwargs):
    for fn in _enabled(_material_handlers):
        result = fn(work_order, **kwargs)
        if result is not None:
            return result
    return None


# Notes beside planned materials: the stock module says how much is on hand and where.

_planned_notes: list[Callable] = []


def planned_note(fn):
    """Register `fn(planned_materials) -> {planned.pk: "note"}`."""
    _planned_notes.append(fn)
    return fn


def annotate_planned(planned) -> list:
    planned = list(planned)
    for p in planned:
        p.stock_note = ""
    for fn in _enabled(_planned_notes):
        notes = fn(planned)
        for p in planned:
            if notes.get(p.pk):
                p.stock_note = notes[p.pk]
    return planned
