from trammel.core.modules import (
    MenuEntry,
    ModuleConfig,
    ModuleManifest,
    Permission,
    SequenceDef,
)


class StockConfig(ModuleConfig):
    name = "trammel.stock"
    label = "stock"
    verbose_name = "Stock"

    manifest = ModuleManifest(
        key="stock",
        name="Stock",
        version="0.3.0",
        description="Stock on hand by location and lot, drawn first in, first out at lot cost.",
        depends_on=("shop", "workorders"),
        urls="trammel.stock.urls",
        url_prefix="stock",
        api_router="trammel.stock.api.router",
        api_prefix="stock",
        permissions=(
            Permission("stock.view", "View stock and its history", read=True),
            Permission("stock.edit", "Receive, move, count and scrap stock"),
            Permission("stock.manage", "Set up locations, tracking and stock settings"),
        ),
        menu=(
            # The materials home: anyone who may see materials sees the list.
            MenuEntry("Stock", "stock:list", "shop.view", "main", 55),
            MenuEntry("Stock locations", "stock:locations", "stock.view", "admin", 27),
            MenuEntry("Stock settings", "stock:settings", "stock.manage", "admin", 27),
        ),
        sequences=(SequenceDef("stock_lot", "Stock lots", "LOT", yearly=False, padding=5),),
        disable_check="trammel.stock.services.disable_problems",
    )
