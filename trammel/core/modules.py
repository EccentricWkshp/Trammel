"""
Module registry (SPEC §4.5, FR-ADM-7).

A module is a Django app whose AppConfig subclasses `ModuleConfig` and carries
a `ModuleManifest`. The manifest declares everything the rest of the system
needs to know about it: dependencies, permissions, menu entries, API router,
and document number series.

Installed means present in the code; every installed module's migrations always
run, so the schema never diverges. Enabled is a row in `ModuleState`. A
disabled module drops out of menus, record-page tabs and search, and keeps its
data: links to its records from elsewhere still open them read-only, because
the permission layer refuses every non-read permission it owns (`access.py`).
"""

from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field

from django.apps import AppConfig, apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

#: Permission actions that only read. Everything else is refused while the
#: owning module is disabled.
READ_ACTIONS = frozenset({"view"})


@dataclass(frozen=True)
class Permission:
    codename: str  # "<module>.<action>", e.g. "customers.edit"
    label: str
    #: A read permission stays grantable while the module is disabled.
    read: bool = False
    #: Granted to customer principals in the portal, always confined to their
    #: own records by the record-level check (FR-PRT-2).
    portal: bool = False
    #: Held only by customers in the portal: never offered in the role editor
    #: or to API keys, and never held by staff, admins included.
    customer_only: bool = False

    @property
    def module(self) -> str:
        return self.codename.split(".", 1)[0]

    @property
    def action(self) -> str:
        return self.codename.split(".", 1)[1]


def crud_permissions(module: str, noun: str, *, approve: bool = False) -> tuple[Permission, ...]:
    """The usual view / create / edit / delete set (SPEC §2), optionally approve."""
    perms = [
        Permission(f"{module}.view", f"View {noun}", read=True),
        Permission(f"{module}.create", f"Create {noun}"),
        Permission(f"{module}.edit", f"Edit {noun}"),
        Permission(f"{module}.delete", f"Delete {noun}"),
    ]
    if approve:
        perms.append(Permission(f"{module}.approve", f"Approve {noun}"))
    return tuple(perms)


@dataclass(frozen=True)
class MenuEntry:
    label: str
    url_name: str
    permission: str | None = None
    #: "main" for the primary navigation, "admin" for the Settings area,
    #: "floor" for the shop-floor app's top bar.
    section: str = "main"
    order: int = 100
    #: Shown only to the system admin (users, roles, modules, system).
    admin_only: bool = False
    #: Left out while this module is enabled, because it offers the same page in
    #: a fuller form (stock lists the materials).
    hidden_with: str | None = None


@dataclass(frozen=True)
class SequenceDef:
    """A document number series this module issues, with its default prefix."""

    key: str
    label: str
    prefix: str
    yearly: bool = True
    padding: int = 4


@dataclass(frozen=True)
class ModuleManifest:
    key: str
    name: str
    version: str
    description: str = ""
    core: bool = False
    default_enabled: bool = True
    depends_on: tuple[str, ...] = ()
    permissions: tuple[Permission, ...] = ()
    menu: tuple[MenuEntry, ...] = ()
    sequences: tuple[SequenceDef, ...] = ()
    #: Dotted path to the module's urlconf, mounted at /<url_prefix>/.
    urls: str | None = None
    url_prefix: str | None = None
    #: Dotted path to a django-ninja Router, mounted at /api/v1/<api_prefix>/.
    api_router: str | None = None
    api_prefix: str | None = None
    #: When False, the module's URLs do not resolve at all while it is disabled
    #: (404), rather than serving read-only pages. Used by the customer portal
    #: and inbound webhooks (FR-PRT-5, FR-IMP-4).
    routes_when_disabled: bool = True
    #: Models whose existence locks the instance currency (SPEC §4.4), as
    #: "app_label.ModelName".
    financial_models: tuple[str, ...] = field(default_factory=tuple)
    #: Records files can be attached to (FR-DOC-1), as ("app_label.Model",
    #: "<view permission>"). Read by the files module when it is installed.
    attachable: tuple[tuple[str, str], ...] = ()
    #: Dotted path to a function that creates starting data (a default
    #: template, say) if it is missing. Runs after every migrate; must be
    #: idempotent and must never overwrite what the admin has changed.
    seed: str | None = None
    #: Dotted path to a function returning reasons the module cannot be
    #: disabled now (stock still tracked, say); empty when it can.
    disable_check: str | None = None


class ModuleConfig(AppConfig):
    """AppConfig base for every Trammel module."""

    manifest: ModuleManifest

    def ready(self) -> None:
        super().ready()
        perms = {p.codename for p in self.manifest.permissions}
        for codename in perms:
            if not codename.startswith(f"{self.manifest.key}."):
                raise ValueError(
                    f"Module {self.manifest.key!r} declares permission {codename!r} "
                    "outside its own namespace."
                )
        # Register the module's background tasks, cross-module hooks and template help.
        from importlib import import_module
        from importlib.util import find_spec

        for submodule in ("tasks", "wiring", "placeholders"):
            if find_spec(f"{self.name}.{submodule}") is not None:
                import_module(f"{self.name}.{submodule}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def manifests() -> list[ModuleManifest]:
    """Every installed module, core first, in INSTALLED_APPS order."""
    found = [cfg.manifest for cfg in apps.get_app_configs() if isinstance(cfg, ModuleConfig)]
    return sorted(found, key=lambda m: not m.core)


def get_manifest(key: str) -> ModuleManifest:
    for manifest in manifests():
        if manifest.key == key:
            return manifest
    raise LookupError(f"No installed module {key!r}.")


def all_permissions() -> dict[str, Permission]:
    return {p.codename: p for m in manifests() for p in m.permissions}


def check_dependencies() -> list[str]:
    """Problems with declared dependencies; empty when the install is coherent."""
    keys = {m.key for m in manifests()}
    problems = []
    for m in manifests():
        for dep in m.depends_on:
            if dep not in keys:
                problems.append(f"{m.key} depends on {dep}, which is not installed.")
            elif m.core and not get_manifest(dep).core:
                problems.append(f"Core module {m.key} cannot depend on optional module {dep}.")
    return problems


# ---------------------------------------------------------------------------
# Enabled state
# ---------------------------------------------------------------------------

#: Per-request cache of enabled module keys, set by middleware so a page that
#: checks dozens of permissions asks the database once.
_request_cache: ContextVar[frozenset[str] | None] = ContextVar("enabled_modules", default=None)


def forced_off() -> frozenset[str]:
    """Modules the server's configuration keeps off, whatever Settings says: the
    customer portal while PORTAL_OFF is set (FR-PRT-8)."""
    from django.conf import settings

    return frozenset({"portal"}) if getattr(settings, "PORTAL_OFF", False) else frozenset()


def _load_enabled() -> frozenset[str]:
    from .models import ModuleState

    states = dict(ModuleState.objects.values_list("key", "enabled"))
    off = forced_off()
    return frozenset(
        m.key
        for m in manifests()
        if m.key not in off and (m.core or states.get(m.key, m.default_enabled))
    )


def enabled_keys() -> frozenset[str]:
    cached = _request_cache.get()
    return cached if cached is not None else _load_enabled()


def is_enabled(key: str) -> bool:
    return key in enabled_keys()


def begin_request_cache() -> object:
    return _request_cache.set(_load_enabled())


def end_request_cache(token: object) -> None:
    _request_cache.reset(token)  # type: ignore[arg-type]


def dependents_of(key: str, among: Iterable[str]) -> list[ModuleManifest]:
    return [m for m in manifests() if key in m.depends_on and m.key in among]


@transaction.atomic
def set_enabled(key: str, enabled: bool, *, reason: str = "") -> None:
    """Enable or disable a module, enforcing dependencies (SPEC §4.5)."""
    from . import audit
    from .models import ModuleState

    manifest = get_manifest(key)
    current = enabled_keys()
    if manifest.core and not enabled:
        raise ValidationError(f"{manifest.name} is a core module and cannot be disabled.")
    if enabled and key in forced_off():
        raise ValidationError(
            f"{manifest.name} is switched off in the server's configuration. Remove that "
            "setting and restart Trammel to turn it on here."
        )
    if enabled:
        missing = [d for d in manifest.depends_on if d not in current]
        if missing:
            names = ", ".join(get_manifest(d).name for d in missing)
            raise ValidationError(f"Enable {names} first; {manifest.name} depends on it.")
    else:
        blocking = dependents_of(key, current)
        if blocking:
            names = ", ".join(m.name for m in blocking)
            raise ValidationError(f"Disable {names} first; it depends on {manifest.name}.")
        if manifest.disable_check and key in current:
            from django.utils.module_loading import import_string

            problems = list(import_string(manifest.disable_check)())
            if problems:
                raise ValidationError(problems)

    if (key in current) == enabled:
        return
    ModuleState.objects.update_or_create(
        key=key, defaults={"enabled": enabled, "changed_at": timezone.now()}
    )
    _request_cache.set(None)
    audit.record(
        "module.enable" if enabled else "module.disable",
        entity_type="core.module",
        entity_id=key,
        entity_label=manifest.name,
        summary=f"{'Enabled' if enabled else 'Disabled'} module {manifest.name}",
        reason=reason,
    )


def sync_installed() -> None:
    """Create state rows and number series for newly installed modules.

    Runs after migrate. Existing rows are left alone, so a prefix the admin has
    changed is never reset.
    """
    from .models import DocumentSequence, ModuleState

    for m in manifests():
        ModuleState.objects.get_or_create(
            key=m.key, defaults={"enabled": m.core or m.default_enabled}
        )
        for seq in m.sequences:
            DocumentSequence.objects.get_or_create(
                key=seq.key,
                defaults={
                    "label": seq.label,
                    "prefix": seq.prefix,
                    "yearly": seq.yearly,
                    "padding": seq.padding,
                },
            )
        if m.seed:
            from django.utils.module_loading import import_string

            import_string(m.seed)()


def currency_locked() -> bool:
    """True once any financial document exists (SPEC §4.4)."""
    for manifest in manifests():
        for label in manifest.financial_models:
            model = apps.get_model(label)
            if getattr(model, "all_objects", model._base_manager).exists():
                return True
    return False


def module_of(python_path: str) -> str | None:
    """The key of the module whose code `python_path` (e.g. `fn.__module__`) is in."""
    config = apps.get_containing_app_config(python_path)
    manifest = getattr(config, "manifest", None)
    return manifest.key if manifest else None
