"""Where the list of materials, and the form that adds one, live.

The materials module has its own list and form. A module that handles materials
more fully (stock: what is on hand, where, when to restock) registers its pages
here, and while that module is enabled they are where people are sent.
"""

from __future__ import annotations

from django.urls import reverse

_home: list[tuple[str, str]] = []
_create: list[tuple[str, str]] = []


def materials_home_is(url_name: str, module_key: str) -> None:
    _home.append((url_name, module_key))


def material_form_is(url_name: str, module_key: str) -> None:
    _create.append((url_name, module_key))


def _first(pages: list[tuple[str, str]], default: str) -> str:
    from trammel.core import modules

    on = modules.enabled_keys()
    for url_name, key in pages:
        if key in on:
            return reverse(url_name)
    return reverse(default)


def materials_home() -> str:
    return _first(_home, "shop:materials")


def add_material_url() -> str:
    return _first(_create, "shop:material_create")
