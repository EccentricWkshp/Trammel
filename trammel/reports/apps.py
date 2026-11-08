from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission


class ReportsConfig(ModuleConfig):
    name = "trammel.reports"
    label = "reports"
    verbose_name = "Reports"

    manifest = ModuleManifest(
        key="reports",
        name="Reports",
        version="0.1.0",
        description="Profitability, sales, receivables, tax, time and force-closed jobs.",
        depends_on=("billing", "time", "workorders", "quotes"),
        urls="trammel.reports.urls",
        url_prefix="reports",
        permissions=(Permission("reports.view", "View reports", read=True),),
        menu=(MenuEntry("Reports", "reports:index", "reports.view", "main", 90),),
    )
