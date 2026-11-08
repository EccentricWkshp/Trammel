"""Menus and URL mounting, both driven by module manifests (SPEC §4.5)."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

from django.urls import NoReverseMatch, URLResolver, include, path, reverse

from . import access, modules


@dataclass(frozen=True)
class MenuItem:
    label: str
    url: str
    url_name: str
    group: str = ""


#: Settings menu headings, by `MenuEntry.order` (lower bound, heading).
ADMIN_GROUPS = [
    (0, "Business"),
    (25, "Pricing and tax"),
    (28, "Documents"),
    (40, "People and access"),
    (60, "System"),
]


def _group(order: int) -> str:
    return next(name for bound, name in reversed(ADMIN_GROUPS) if order >= bound)


def menu_for(actor, section: str = "main") -> list[MenuItem]:
    """Entries the actor may open. A disabled module has none: it is out of the way."""
    enabled = modules.enabled_keys()
    items: list[tuple[int, MenuItem]] = []
    for manifest in modules.manifests():
        if manifest.key not in enabled:
            continue
        for entry in manifest.menu:
            if entry.section != section:
                continue
            if entry.admin_only and not access.is_admin(actor):
                continue
            if entry.permission and not access.has_perm(actor, entry.permission):
                continue
            try:
                url = reverse(entry.url_name)
            except NoReverseMatch:
                continue
            items.append(
                (
                    entry.order,
                    MenuItem(
                        entry.label,
                        url,
                        entry.url_name,
                        _group(entry.order) if section == "admin" else "",
                    ),
                )
            )
    return [item for _, item in sorted(items, key=lambda pair: pair[0])]


class ModuleGatedResolver(URLResolver):
    """Resolves nothing while its module is disabled, so its routes are 404 —
    not hidden, absent (FR-PRT-5, FR-IMP-4)."""

    def __init__(self, module_key: str, *args, **kwargs):
        self.module_key = module_key
        super().__init__(*args, **kwargs)

    def resolve(self, path):
        if not modules.is_enabled(self.module_key):
            return None
        return super().resolve(path)


def module_urlpatterns() -> list:
    patterns = []
    for manifest in modules.manifests():
        if not manifest.urls:
            continue
        prefix = f"{manifest.url_prefix}/" if manifest.url_prefix else ""
        included = include(manifest.urls)
        if manifest.routes_when_disabled:
            patterns.append(path(prefix, included))
        else:
            urlconf, app_name, namespace = included
            resolver = path(prefix, included)
            patterns.append(
                ModuleGatedResolver(
                    manifest.key,
                    resolver.pattern,
                    urlconf,
                    app_name=app_name,
                    namespace=namespace,
                )
            )
    return patterns


def mount_api_routers(api) -> None:
    for manifest in modules.manifests():
        if manifest.api_router:
            module_path, _, attr = manifest.api_router.rpartition(".")
            router = getattr(import_module(module_path), attr)
            api.add_router(f"/{manifest.api_prefix or manifest.key}/", router)
