from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest


class NotificationsConfig(ModuleConfig):
    name = "trammel.notifications"
    label = "notifications"
    verbose_name = "Email notifications"

    manifest = ModuleManifest(
        key="notifications",
        name="Email",
        version="0.1.0",
        description="Email through your own SMTP server: quotes, invoices, receipts, reminders.",
        depends_on=("quotes", "billing"),
        urls="trammel.notifications.urls",
        url_prefix="settings/email",
        menu=(
            MenuEntry("Email", "notifications:smtp", section="admin", order=65, admin_only=True),
            MenuEntry("Email log", "notifications:log", section="admin", order=66, admin_only=True),
        ),
    )
