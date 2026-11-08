from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission


class CustomersConfig(ModuleConfig):
    name = "trammel.customers"
    label = "customers"
    verbose_name = "Customers"

    manifest = ModuleManifest(
        key="customers",
        name="Customers",
        version="0.1.0",
        description="Customers with contacts, addresses and tax exemptions.",
        core=True,
        urls="trammel.customers.urls",
        url_prefix="customers",
        api_router="trammel.customers.api.router",
        attachable=(("customers.Customer", "customers.view"),),
        permissions=(
            Permission("customers.view", "View customers", read=True, portal=True),
            Permission("customers.create", "Create customers"),
            Permission("customers.edit", "Edit customers"),
            Permission("customers.delete", "Delete customers"),
        ),
        menu=(MenuEntry("Customers", "customers:list", "customers.view", "main", 10),),
    )
