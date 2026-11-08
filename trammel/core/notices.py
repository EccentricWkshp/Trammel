"""Banners shown at the top of every page — a stale backup, an available update.

Modules register `fn(request) -> list[Notice]`; core collects them without
importing the modules that raise them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

_providers: list[Callable] = []


@dataclass(frozen=True)
class Notice:
    level: str  # "warning" or "info"
    text: str
    url: str = ""


def register(fn: Callable) -> Callable:
    _providers.append(fn)
    return fn


def for_request(request) -> list[Notice]:
    return [n for fn in _providers for n in fn(request)]
