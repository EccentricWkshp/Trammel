from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest


class EdgeConfig(ModuleConfig):
    name = "trammel.edge"
    label = "edge"
    verbose_name = "Edge portal"

    manifest = ModuleManifest(
        key="edge",
        name="Edge portal (Cloudflare)",
        version="0.1.0",
        description="Serves the customer portal from Cloudflare, so Trammel never has to be "
        "reachable from the internet: Trammel publishes what customers may see and pulls back "
        "what they did.",
        default_enabled=False,
        depends_on=("portal",),
        urls="trammel.edge.urls",
        url_prefix="settings/edge-portal",
        menu=(
            MenuEntry(
                "Portal on Cloudflare", "edge:settings", section="admin", order=66, admin_only=True
            ),
        ),
    )
