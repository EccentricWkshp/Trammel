"""What a scanned code opens (FR-STK-7).

Labels carry a short link, `/s/<id>/`, to the record they are stuck on. Each
module registers how to open its own records, so core never imports a module:

    @scan.record
    def open_lot(request, pk): return url or None

Codes that are not links — a work order number typed in, a lot number, a
manufacturer's barcode scanned with a hand scanner — go through text finders:

    @scan.text
    def by_barcode(request, code): return url or None

A finder returns None when the code is not one of its own, and the next is
tried. Finders of disabled modules are skipped.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable

from django.conf import settings
from django.urls import reverse

_records: list[Callable] = []
_texts: list[Callable] = []
_unknown: list[Callable] = []

LINK = re.compile(r"/s/([0-9a-fA-F-]{36})/?")


def record(fn: Callable) -> Callable:
    """Register `fn(request, pk) -> url | None` for opening a record by its id."""
    _records.append(fn)
    return fn


def text(fn: Callable) -> Callable:
    """Register `fn(request, code) -> url | None` for a code typed or scanned."""
    _texts.append(fn)
    return fn


def unknown(fn: Callable) -> Callable:
    """Register `fn(request, code) -> (url, message) | None`: what to offer when
    nothing matched (stock offers to add a material with that barcode)."""
    _unknown.append(fn)
    return fn


def link(pk) -> str:
    """The absolute short link a label's QR code carries."""
    return settings.BASE_URL + reverse("core:scan_open", args=[pk])


def _enabled(fns: list[Callable]) -> list[Callable]:
    from . import modules

    on = modules.enabled_keys()
    return [f for f in fns if (key := modules.module_of(f.__module__)) is None or key in on]


def open_record(request, pk) -> str | None:
    for fn in _enabled(_records):
        if url := fn(request, pk):
            return url
    return None


def find(request, code: str) -> str | None:
    """Where a scanned or typed code leads, or None."""
    code = code.strip()
    if not code:
        return None
    if match := LINK.search(code):
        try:
            return open_record(request, uuid.UUID(match.group(1)))
        except ValueError:
            return None
    for fn in _enabled(_texts):
        if url := fn(request, code):
            return url
    return None


def offer(request, code: str) -> tuple[str, str] | None:
    for fn in _enabled(_unknown):
        if result := fn(request, code.strip()):
            return result
    return None
