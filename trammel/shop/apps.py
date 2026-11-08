from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission


class ShopConfig(ModuleConfig):
    name = "trammel.shop"
    label = "shop"
    verbose_name = "Machines and materials"

    manifest = ModuleManifest(
        key="shop",
        name="Machines and materials",
        version="0.1.0",
        description="Machines with hourly rates, and the materials price list used for quoting.",
        urls="trammel.shop.urls",
        url_prefix="shop",
        api_router="trammel.shop.api.router",
        attachable=(
            ("shop.Machine", "shop.view"),
            ("shop.Material", "shop.view"),
            ("shop.Vendor", "shop.view"),
        ),
        permissions=(
            Permission("shop.view", "View machines, materials and vendors", read=True),
            Permission("shop.edit", "Add and edit machines, materials and vendors"),
        ),
        menu=(
            MenuEntry("Machines", "shop:machines", "shop.view", "admin", 25),
            MenuEntry("Materials", "shop:materials", "shop.view", "admin", 26),
            MenuEntry("Vendors", "shop:vendors", "shop.view", "admin", 27),
        ),
    )
