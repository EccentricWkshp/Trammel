from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission


class AccountsConfig(ModuleConfig):
    name = "trammel.accounts"
    label = "accounts"
    verbose_name = "Users and roles"

    manifest = ModuleManifest(
        key="accounts",
        name="Users and roles",
        version="0.1.0",
        description="Sign-in, users, user-defined roles and API keys.",
        core=True,
        urls="trammel.accounts.urls",
        url_prefix="accounts",
        permissions=(Permission("accounts.api_keys", "Create personal API keys"),),
        menu=(
            MenuEntry("Users", "accounts:users", section="admin", order=40, admin_only=True),
            MenuEntry("Roles", "accounts:roles", section="admin", order=50, admin_only=True),
        ),
    )
