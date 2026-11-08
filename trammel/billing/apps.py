from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission, SequenceDef


class BillingConfig(ModuleConfig):
    name = "trammel.billing"
    label = "billing"
    verbose_name = "Invoicing"

    manifest = ModuleManifest(
        key="billing",
        name="Invoicing and payments",
        version="0.1.0",
        description="Invoices, direct sales, credit notes, recorded payments and write-offs.",
        depends_on=("jobs", "quotes", "workorders", "time", "catalog", "tax"),
        urls="trammel.billing.urls",
        url_prefix="invoices",
        api_router="trammel.billing.api.router",
        api_prefix="invoices",
        seed="trammel.billing.seed.seed",
        attachable=(("billing.Invoice", "billing.view"),),
        permissions=(
            Permission("billing.view", "View invoices and payments", read=True, portal=True),
            Permission("billing.create", "Create and edit draft invoices and sales"),
            Permission("billing.issue", "Issue invoices and credit notes"),
            Permission("billing.void", "Void invoices"),
            Permission("billing.record_payments", "Record payments and apply credit"),
            Permission("billing.write_off", "Write off unpaid balances"),
            Permission("billing.manage_templates", "Edit invoice templates"),
        ),
        menu=(
            MenuEntry("Invoices", "billing:list", "billing.view", "main", 70),
            MenuEntry(
                "Invoice templates", "billing:templates", "billing.manage_templates", "admin", 31
            ),
        ),
        sequences=(
            SequenceDef("invoice", "Invoices", "INV"),
            SequenceDef("credit_note", "Credit notes", "CN"),
        ),
        financial_models=("billing.Invoice", "billing.Payment"),
    )
