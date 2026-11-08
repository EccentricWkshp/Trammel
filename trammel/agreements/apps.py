from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission


class AgreementsConfig(ModuleConfig):
    name = "trammel.agreements"
    label = "agreements"
    verbose_name = "Customer agreements"

    manifest = ModuleManifest(
        key="agreements",
        name="Customer agreements",
        version="0.1.0",
        description="Terms and conditions accepted for each job before work starts.",
        depends_on=("jobs", "files"),
        urls="trammel.agreements.urls",
        url_prefix="agreements",
        seed="trammel.agreements.seed.seed",
        permissions=(
            Permission("agreements.view", "View customer agreements", read=True, portal=True),
            Permission("agreements.manage", "Issue agreements, record acceptance, edit templates"),
        ),
        menu=(MenuEntry("Agreements", "agreements:templates", "agreements.view", "admin", 28),),
    )
