from trammel.core.modules import MenuEntry, ModuleConfig, ModuleManifest, Permission


class FilesConfig(ModuleConfig):
    name = "trammel.files"
    label = "files"
    verbose_name = "Files"

    manifest = ModuleManifest(
        key="files",
        name="Files",
        version="0.1.0",
        description="Drawings, photos, DXF, LightBurn, STL/3MF and G-code attached to records.",
        urls="trammel.files.urls",
        url_prefix="files",
        api_router="trammel.files.api.router",
        permissions=(
            Permission("files.view", "View and download files", read=True, portal=True),
            Permission("files.upload", "Upload, attach and detach files"),
            Permission("files.manage_types", "Manage document types"),
        ),
        menu=(MenuEntry("Document types", "files:types", "files.manage_types", "admin", 32),),
        seed="trammel.files.seed.seed",
    )
