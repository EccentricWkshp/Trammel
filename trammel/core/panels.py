"""Sections modules add to another module's record page, and the tabs that hold them.

The customer page shows jobs, quotes and invoices (FR-CUS-4); the job page shows
quotes, work orders and invoices — each contributed by its own module, so no
module imports one that depends on it.

    @panels.register("customers.customer")
    def customer_jobs(request, customer): return Panel(..., tab="jobs") or None

A panel with a `tab` gets its own tab on the record page; one without is shown
on the Overview tab. Tabs are plain links (`?tab=jobs`) rendered on the server:
no script, bookmarkable, and the Back button works.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

_providers: dict[str, list[Callable]] = defaultdict(list)


@dataclass
class Panel:
    title: str
    template: str
    context: dict = field(default_factory=dict)
    order: int = 100
    #: Own tab key, or None to appear on the Overview tab.
    tab: str | None = None
    #: Shown beside the tab label, e.g. how many invoices.
    count: int | None = None


def register(model_label: str):
    def decorator(fn: Callable):
        _providers[model_label.lower()].append(fn)
        return fn

    return decorator


def for_object(request, obj) -> list[Panel]:
    from . import modules

    enabled = modules.enabled_keys()
    found = [
        p
        for fn in _providers[obj._meta.label_lower]
        if (key := modules.module_of(fn.__module__)) is None or key in enabled
        if (p := fn(request, obj)) is not None
    ]
    return sorted(found, key=lambda p: p.order)


def tabbed(
    request,
    panels: Iterable[Panel],
    *,
    first: Iterable[tuple[str, str]] = (("overview", "Overview"),),
    last: Iterable[tuple[str, str]] = (),
) -> dict:
    """Template context for a tabbed record page.

    `first` and `last` are the page's own tabs (key, label); panel tabs go
    between them. Returns `tabs`, the active `tab`, `overview_panels` (panels
    without a tab) and `tab_panels` (the active tab's panels).
    """
    panels = list(panels)
    entries = [(k, label, None) for k, label in first]
    entries += [(p.tab, p.title, p.count) for p in panels if p.tab]
    entries += [(k, label, None) for k, label in last]
    keys = [k for k, _, _ in entries]
    active = request.GET.get("tab", "")
    if active not in keys:
        active = keys[0]
    return {
        "tabs": [
            {"key": k, "label": label, "count": count, "active": k == active}
            for k, label, count in entries
        ],
        "tab": active,
        "overview_panels": [p for p in panels if not p.tab],
        "tab_panels": [p for p in panels if p.tab == active],
    }
