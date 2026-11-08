from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission, SequenceDef


class WorkOrdersConfig(ModuleConfig):
    name = "trammel.workorders"
    label = "workorders"
    verbose_name = "Work orders"

    manifest = ModuleManifest(
        key="workorders",
        name="Work orders",
        version="0.1.0",
        description="Work orders, operations, travelers, material use and change orders.",
        depends_on=("jobs", "quotes", "agreements", "files", "shop"),
        urls="trammel.workorders.urls",
        url_prefix="work-orders",
        api_router="trammel.workorders.api.router",
        api_prefix="work-orders",
        attachable=(("workorders.WorkOrder", "workorders.view"),),
        permissions=(
            Permission("workorders.view", "View work orders", read=True, portal=True),
            Permission("workorders.create", "Create work orders"),
            Permission(
                "workorders.edit",
                "Work on work orders: operations, notes, materials, status; change orders",
            ),
            Permission("workorders.approve", "Record change order approval"),
            Permission("workorders.reopen", "Reopen work orders"),
        ),
        menu=(MenuEntry("Work orders", "workorders:list", "workorders.view", "main", 40),),
        sequences=(
            SequenceDef("work_order", "Work orders", "WO"),
            SequenceDef("change_order", "Change orders", "CO"),
        ),
    )
