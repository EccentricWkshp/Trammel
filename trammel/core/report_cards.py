"""Reports other modules add to the Reports page (FR-RPT).

The reports module lists its own; stock and purchasing, which it does not
depend on, register theirs here:

    report_cards.register("Stock valuation", "What stock is worth…", "stock:valuation",
                          permission="stock.view")

A card shows while its module (the URL's namespace) is enabled and the person
holds its permission.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse


@dataclass(frozen=True)
class Card:
    title: str
    description: str
    url_name: str
    permission: str
    order: int

    @property
    def url(self) -> str:
        return reverse(self.url_name)


_cards: list[Card] = []


def register(
    title: str, description: str, url_name: str, *, permission: str, order: int = 100
) -> None:
    _cards.append(Card(title, description, url_name, permission, order))


def for_actor(actor) -> list[Card]:
    from . import access, modules

    on = modules.enabled_keys()
    return sorted(
        (
            c
            for c in _cards
            if c.url_name.split(":", 1)[0] in on and access.has_perm(actor, c.permission)
        ),
        key=lambda c: (c.order, c.title),
    )
