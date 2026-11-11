from trammel.core.modules import ModuleConfig, ModuleManifest, Permission


class PortalConfig(ModuleConfig):
    name = "trammel.portal"
    label = "portal"
    verbose_name = "Customer portal"

    manifest = ModuleManifest(
        key="portal",
        name="Customer portal",
        version="0.1.0",
        description="Customers sign in with an emailed code to see their jobs, quotes and "
        "invoices, and approve or decline quotes.",
        # Off until the admin turns it on: it is only of use once Trammel is
        # reachable from the internet (SPEC §4).
        default_enabled=False,
        depends_on=("jobs", "quotes", "notifications"),
        urls="trammel.portal.urls",
        url_prefix="portal",
        # While it is off, its addresses don't exist at all (FR-PRT-5).
        routes_when_disabled=False,
        permissions=(Permission("portal.manage", "Give customers access to the portal"),),
    )
