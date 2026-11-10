"""Core's own home screen section: shortcuts to each area the user can open."""

from __future__ import annotations

from . import dashboard
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
