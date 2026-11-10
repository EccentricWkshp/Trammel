from django.conf import settings

from .navigation import menu_for


def _active(items, path: str) -> str:
    """The URL of the one menu item to highlight: the longest that the path is under."""
    matches = [i.url for i in items if path.startswith(i.url) and i.url != "/"]
    return max(matches, key=len, default="")


def instance(request):
    actor = getattr(request, "actor", None)
    business = getattr(request, "business", None)
    ctx = {
        "app_version": settings.APP_VERSION,
        "business": business,
    }
    if actor is not None and actor.is_authenticated:
        from .notices import for_request

        main, admin = menu_for(actor, "main"), menu_for(actor, "admin")
        ctx["main_menu"] = main
        ctx["admin_menu"] = admin
        ctx["active_main_url"] = _active(main, request.path)
        ctx["active_admin_url"] = _active(admin, request.path)
        # Buttons in the shop-floor app's top bar that other modules add (stock).
        ctx["floor_menu"] = menu_for(actor, "floor")
        ctx["notices"] = for_request(request)
    return ctx
