from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission, SequenceDef


class QuotesConfig(ModuleConfig):
    name = "trammel.quotes"
    label = "quotes"
    verbose_name = "Quotes"

    manifest = ModuleManifest(
        key="quotes",
        name="Quotes",
        version="0.1.0",
        description="Quotes and change orders with revisions, estimators, templates and approval.",
        depends_on=("jobs", "shop", "catalog", "tax", "agreements"),
        urls="trammel.quotes.urls",
        url_prefix="quotes",
        api_router="trammel.quotes.api.router",
        seed="trammel.quotes.seed.seed",
        attachable=(("quotes.Quote", "quotes.view"),),
        permissions=(
            Permission("quotes.view", "View quotes", read=True, portal=True),
            Permission("quotes.create", "Create quotes and change orders"),
            Permission(
                "quotes.edit",
                "Edit, send, decline and revise quotes and change orders, and delete unsent ones",
            ),
            Permission("quotes.approve", "Record quote approval"),
            Permission("quotes.approve_change_orders", "Record change order approval"),
            Permission("quotes.manage_templates", "Edit quote templates and settings"),
        ),
        menu=(
            MenuEntry("Quotes", "quotes:list", "quotes.view", "main", 30),
            MenuEntry(
                "Quote templates", "quotes:templates", "quotes.manage_templates", "admin", 29
            ),
        ),
        sequences=(
            SequenceDef("quote", "Quotes", "Q"),
            SequenceDef("change_order", "Change orders", "CO"),
        ),
        financial_models=("quotes.QuoteRevision",),
    )
