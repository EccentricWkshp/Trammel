from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest


class SystemConfig(ModuleConfig):
    name = "trammel.system"
    label = "system"
    verbose_name = "Maintenance"

    manifest = ModuleManifest(
        key="system",
        name="Backups, export and updates",
        version="0.1.0",
        description="Backups and restore, full data export, the update check and the trash.",
        core=True,
        urls="trammel.system.urls",
        url_prefix="settings/maintenance",
        menu=(
            MenuEntry(
                "Backups and export",
                "system:maintenance",
                section="admin",
                order=75,
                admin_only=True,
            ),
            MenuEntry("Trash", "system:trash", section="admin", order=80, admin_only=True),
        ),
    )
