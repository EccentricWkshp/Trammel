"""What other modules add to emails, without the notifications module importing them."""

from __future__ import annotations

from collections.abc import Callable

#: fn(record) -> str: an address where the customer can see `record` (a quote
#: revision or an invoice) online, or "" — the customer portal registers one.
online_links: list[Callable] = []


def online_link(record) -> str:
    for fn in online_links:
        if url := fn(record):
            return url
    return ""
