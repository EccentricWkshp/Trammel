from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission


class TaxConfig(ModuleConfig):
    name = "trammel.tax"
    label = "tax"
    verbose_name = "Tax"

    manifest = ModuleManifest(
        key="tax",
        name="Tax",
        version="0.1.0",
        description="Sales tax rates by address (manual, CSV, SST files) and gross receipts taxes.",
        urls="trammel.tax.urls",
        url_prefix="tax",
        api_router="trammel.tax.api.router",
        permissions=(
            Permission("tax.view", "View tax rates and jurisdictions", read=True),
            Permission("tax.manage", "Manage tax rates, categories and imports"),
        ),
        menu=(MenuEntry("Tax", "tax:overview", "tax.view", "admin", 27),),
    )
