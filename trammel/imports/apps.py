from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission


class ImportsConfig(ModuleConfig):
    name = "trammel.imports"
    label = "imports"
    verbose_name = "Online orders"

    manifest = ModuleManifest(
        key="imports",
        name="Online orders",
        version="0.1.0",
        description="Orders from online stores (Shopify) brought in as paid sales, with "
        "their customers, and a job when they need making.",
        # Off until someone connects a store.
        default_enabled=False,
        depends_on=("billing", "catalog", "quotes", "jobs"),
        urls="trammel.imports.urls",
        url_prefix="imports",
        # Its webhook is an address on the internet; while the module is off it
        # doesn't exist (FR-IMP-4).
        routes_when_disabled=False,
        permissions=(
            Permission("imports.view", "See imported online orders", read=True),
            Permission(
                "imports.run",
                "Fetch online orders, complete ones waiting for a customer, and make jobs "
                "from them",
            ),
            Permission("imports.manage", "Connect online stores"),
        ),
        menu=(
            MenuEntry("Online orders", "imports:list", "imports.view", "main", 75),
            MenuEntry("Online stores", "imports:settings", "imports.manage", "admin", 64),
        ),
    )
