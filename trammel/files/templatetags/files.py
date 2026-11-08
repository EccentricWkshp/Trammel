from django import template

from trammel.core import access

from .. import services

register = template.Library()


@register.inclusion_tag("files/_attachments.html", takes_context=True)
def attachments(context, obj, title="Files"):
    request = context["request"]
    perm = services.target_permission(obj)
    can_see = access.has_perm(request.actor, "files.view")
    return {
        "request": request,
        "csrf_token": context.get("csrf_token"),
        "title": title,
        "target": obj,
        "label": obj._meta.label_lower,
        "links": services.attachments_for(obj) if can_see else [],
        "can_see": can_see,
        "can_upload": perm is not None and access.has_perm(request.actor, "files.upload"),
        "here": request.get_full_path(),
    }
