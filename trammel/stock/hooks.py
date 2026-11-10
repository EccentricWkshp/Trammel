"""What later modules add to stock pages (purchasing: quantities on order).

@hooks.on_order
def ordered(item_ids) -> {item_id: quantity in the stock unit}

hooks.restock_action("Create purchase orders", "purchasing:from_restock")
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from django.urls import reverse

_on_order: list[Callable] = []
_restock_actions: list[tuple[str, str]] = []


def on_order(fn: Callable) -> Callable:
    _on_order.append(fn)
    return fn


def restock_action(label: str, url_name: str) -> None:
    _restock_actions.append((label, url_name))


def _enabled(fns):
    from trammel.core import modules

    on = modules.enabled_keys()
    return [f for f in fns if (key := modules.module_of(f.__module__)) is None or key in on]


def tracks_orders() -> bool:
    """Whether an enabled module reports quantities on order."""
    return bool(_enabled(_on_order))


def ordered(item_ids) -> dict:
    """Quantity on order per stock item, in its stock unit."""
    totals: dict = {}
    for fn in _enabled(_on_order):
        for key, qty in fn(list(item_ids)).items():
            totals[key] = totals.get(key, Decimal("0")) + qty
    return totals


def restock_actions(actor) -> list[tuple[str, str]]:
    from trammel.core import access, modules

    on = modules.enabled_keys()
    out = []
    for label, url_name in _restock_actions:
        module = url_name.split(":", 1)[0]
        if module in on and access.has_perm(actor, f"{module}.create"):
            out.append((label, reverse(url_name)))
    return out
