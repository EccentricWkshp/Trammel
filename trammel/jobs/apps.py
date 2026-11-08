from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission, SequenceDef


class JobsConfig(ModuleConfig):
    name = "trammel.jobs"
    label = "jobs"
    verbose_name = "Jobs"

    manifest = ModuleManifest(
        key="jobs",
        name="Jobs",
        version="0.1.0",
        description="Jobs from proposal to closeout: dates, holds, reopen, forced closeout.",
        depends_on=("customers",),
        urls="trammel.jobs.urls",
        url_prefix="jobs",
        api_router="trammel.jobs.api.router",
        attachable=(("jobs.Job", "jobs.view"),),
        permissions=(
            Permission("jobs.view", "View jobs", read=True, portal=True),
            Permission("jobs.create", "Create jobs"),
            Permission("jobs.edit", "Edit jobs and move them through their lifecycle"),
            Permission("jobs.delete", "Delete jobs (proposal, declined or canceled only)"),
            Permission("jobs.reopen", "Reopen completed or closed jobs"),
            Permission("jobs.force_closeout", "Force closeout of a job"),
        ),
        menu=(
            MenuEntry("Jobs", "jobs:list", "jobs.view", "main", 20),
            MenuEntry("Calendar", "jobs:calendar", "jobs.view", "main", 80),
        ),
        sequences=(SequenceDef("job", "Jobs", "J"),),
    )
