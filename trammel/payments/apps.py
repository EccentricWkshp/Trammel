from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission


class PaymentsConfig(ModuleConfig):
    name = "trammel.payments"
    label = "payments"
    verbose_name = "Online payments"

    manifest = ModuleManifest(
        key="payments",
        name="Online payments",
        version="0.1.0",
        description="Customers pay invoices online, by card through Stripe: Trammel makes a "
        "payment link for each open invoice and records what's paid, with nothing reaching in "
        "to it from the internet.",
        default_enabled=False,
        depends_on=("billing",),
        urls="trammel.payments.urls",
        url_prefix="settings/online-payments",
        api_router="trammel.payments.api.router",
        permissions=(
            Permission(
                "payments.manage",
                "Set up online payments, and see and remake an invoice's payment link",
            ),
        ),
        menu=(
            MenuEntry("Online payments", "payments:settings", "payments.manage", "admin", order=67),
        ),
    )
