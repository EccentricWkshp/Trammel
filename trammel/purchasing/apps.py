from trammel.core.modules import (
    MenuEntry,
    ModuleConfig,
    ModuleManifest,
    Permission,
    SequenceDef,
)


class PurchasingConfig(ModuleConfig):
    name = "trammel.purchasing"
    label = "purchasing"
    verbose_name = "Purchasing"

    manifest = ModuleManifest(
        key="purchasing",
        name="Purchasing",
        version="0.3.3",
        description="Purchase orders to vendors, receiving at landed cost, and buying for a job.",
        depends_on=("stock",),
        urls="trammel.purchasing.urls",
        url_prefix="purchasing",
        api_router="trammel.purchasing.api.router",
        api_prefix="purchasing",
        attachable=(("purchasing.PurchaseOrder", "purchasing.view"),),
        permissions=(
            Permission("purchasing.view", "View purchase orders", read=True),
            Permission("purchasing.create", "Create and edit draft purchase orders"),
            Permission("purchasing.order", "Place, cancel and close purchase orders"),
            Permission("purchasing.receive", "Receive deliveries and undo receipts"),
        ),
        menu=(MenuEntry("Purchasing", "purchasing:list", "purchasing.view", "main", 57),),
        sequences=(SequenceDef("purchase_order", "Purchase orders", "PO"),),
    )
