from django.db.models.signals import post_migrate

from .modules import MenuEntry, ModuleConfig, ModuleManifest, Permission


class CoreConfig(ModuleConfig):
    name = "trammel.core"
    label = "core"
    verbose_name = "Core"

    manifest = ModuleManifest(
        key="core",
        name="Core",
        version="0.1.0",
        description="Settings, audit log, module registry and document numbering.",
        core=True,
        urls="trammel.core.urls",
        url_prefix="",
        api_router="trammel.core.api.router",
        api_prefix="audit",
        permissions=(
            Permission(
                "core.manage_business", "Edit business profile, shop rates and document numbering"
            ),
            Permission("core.view_audit", "View the audit log", read=True),
            Permission("core.see_costs", "See costs and margins", read=True),
        ),
        menu=(
            MenuEntry("Business", "core:business", "core.manage_business", "admin", 10),
            MenuEntry("Shop rates", "core:rates", "core.manage_business", "admin", 20),
            MenuEntry("Document numbers", "core:sequences", "core.manage_business", "admin", 22),
            MenuEntry("Modules", "core:modules", section="admin", order=60, admin_only=True),
            MenuEntry("System", "core:system", section="admin", order=70, admin_only=True),
            MenuEntry("Audit log", "core:audit", "core.view_audit", "admin", 90),
        ),
    )

    def ready(self) -> None:
        super().ready()
        from . import (
            checks,  # noqa: F401 — registers system checks
            search_providers,  # noqa: F401 — registers global search
        )

        post_migrate.connect(_sync_modules, sender=self)


def _sync_modules(**kwargs) -> None:
    from .modules import sync_installed

    sync_installed()
