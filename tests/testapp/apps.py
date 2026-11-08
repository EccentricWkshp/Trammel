from trammel.core.modules import ModuleConfig, ModuleManifest, Permission, SequenceDef


class TestAppConfig(ModuleConfig):
    """A non-core module that exists only in the test suite."""

    name = "tests.testapp"
    label = "testapp"

    manifest = ModuleManifest(
        key="testapp",
        name="Test widgets",
        version="0.0.0",
        depends_on=("customers",),
        permissions=(
            Permission("testapp.view", "View widgets", read=True, portal=True),
            Permission("testapp.edit", "Edit widgets"),
            Permission("testapp.reopen", "Reopen widgets"),
            Permission("testapp.force", "Force-close widgets"),
        ),
        sequences=(
            SequenceDef("widget", "Widget", "W"),
            SequenceDef("ticket", "Ticket", "T", yearly=False, padding=3),
        ),
        financial_models=("testapp.Widget",),
    )
