"""Core's own home screen section: shortcuts to each area the user can open."""

from __future__ import annotations

from django.urls import reverse

from . import access, dashboard, notices
from .navigation import menu_for


@dashboard.register(
    "core.shortcuts",
    "Shortcuts",
    order=900,
    description="A button for each area of Trammel you can open.",
)
def shortcuts(request):
    return dashboard.Section(
        template="core/_home_shortcuts.html",
        rows=menu_for(request.actor, "main"),
        empty="Your role does not include any areas yet. "
        "Ask the system admin to assign you a role.",
    )


@notices.register
def admin_two_factor(request):
    """An action item for admins until two-factor sign-in is required of them:
    on the home page and the settings pages, not every screen. Requiring it is
    the admin's choice, so it is recommended, never forced."""
    actor = getattr(request, "actor", None)
    if not access.is_admin(actor):
        return []
    path = request.path
    if path != "/" and not path.startswith("/settings/"):
        return []
    from .models import SystemSettings

    if SystemSettings.load().require_mfa_for_admins:
        return []
    return [
        notices.Notice(
            "warning",
            "Action item: require two-factor sign-in for system admins. Without it, an admin "
            "can sign in with a password or an emailed link alone, and an admin can do "
            "anything, from downloading backups to changing users. Strongly recommended "
            "before Trammel or its customer portal can be reached from the internet.",
            reverse("core:system"),
        )
    ]
