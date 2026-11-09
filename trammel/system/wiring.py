from django.urls import reverse

from trammel.core import access, notices, panels

from . import backup, updates


@panels.register("core.systemsettings")
def updates_panel(request, system_settings):
    """The update status, on the System page beside the update check switch (§4.3)."""
    from .models import UpdateStatus

    return panels.Panel(
        "Updates",
        "system/_updates_panel.html",
        context={
            "status": UpdateStatus.load(),
            "available": updates.available(),
            "checking": system_settings.update_check_enabled and not system_settings.is_offline,
        },
        order=10,
    )


@notices.register
def admin_notices(request):
    """FR-BAK-2: backup age shown prominently; §4.3: updates announced to the admin."""
    if not access.is_admin(getattr(request, "actor", None)):
        return []
    found = []
    if warning := backup.age_warning():
        found.append(notices.Notice("warning", warning, reverse("system:maintenance")))
    if version := updates.available():
        found.append(
            notices.Notice("info", f"Trammel {version} is available.", reverse("core:system"))
        )
    return found
