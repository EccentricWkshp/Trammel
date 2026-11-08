from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission


class CatalogConfig(ModuleConfig):
    name = "trammel.catalog"
    label = "catalog"
    verbose_name = "Products"

    manifest = ModuleManifest(
        key="catalog",
        name="Products",
        version="0.1.0",
        description="Catalog of made, resold and service items.",
        depends_on=("tax",),
        urls="trammel.catalog.urls",
        url_prefix="products",
        api_router="trammel.catalog.api.router",
        attachable=(("catalog.Product", "catalog.view"),),
        permissions=(
            Permission("catalog.view", "View products", read=True),
            Permission("catalog.edit", "Add and edit products"),
        ),
        menu=(MenuEntry("Products", "catalog:list", "catalog.view", "main", 60),),
    )
