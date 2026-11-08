from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest


class FloorConfig(ModuleConfig):
    name = "trammel.floor"
    label = "floor"
    verbose_name = "Shop floor"

    manifest = ModuleManifest(
        key="floor",
        name="Shop floor app",
        version="0.1.0",
        description="Installable phone/tablet pages: scan travelers, timers, operations, photos.",
        depends_on=("workorders", "time"),
        urls="trammel.floor.urls",
        url_prefix="floor",
        menu=(MenuEntry("Shop floor", "floor:home", "workorders.view", "main", 100),),
    )
