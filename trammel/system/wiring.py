from django.urls import reverse

from trammel.core import access, notices

from . import backup, updates


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
            notices.Notice(
                "info", f"Trammel {version} is available.", reverse("system:maintenance")
            )
        )
    return found
