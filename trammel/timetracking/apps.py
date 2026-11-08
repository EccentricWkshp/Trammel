from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission


class TimeTrackingConfig(ModuleConfig):
    name = "trammel.timetracking"
    label = "timetracking"
    verbose_name = "Time tracking"

    manifest = ModuleManifest(
        key="time",
        name="Time tracking",
        version="0.1.0",
        description="Labor and machine timers and time entries against work orders.",
        depends_on=("workorders",),
        urls="trammel.timetracking.urls",
        url_prefix="time",
        api_router="trammel.timetracking.api.router",
        permissions=(
            Permission("time.view", "View time entries", read=True),
            Permission("time.track", "Run timers and add own time"),
            Permission("time.manage", "Correct anyone's time and stop their timers"),
        ),
        menu=(MenuEntry("Time", "time:list", "time.view", "main", 50),),
    )
