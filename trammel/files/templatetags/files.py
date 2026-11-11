from django import template

from trammel.core import access

from .. import services
from ..models import DocumentType

register = template.Library()


@register.inclusion_tag("files/_preview.html")
def file_preview(revision, file_url, thumb_url, status_url, title="", alt="", lazy=True):
    """A file's preview on a card: its picture, opening over the page (a PDF in a
    new tab); while it is being made, a placeholder the page swaps for it when
    it's ready (previews.js); otherwise the kind of file."""
    blob = revision.blob
    inline = blob.kind in ("image", "svg", "pdf")
    return {
        "ready": blob.thumb_state == "ready" or blob.kind == "svg",
        "waiting": blob.thumb_state == "pending",
        "kind": blob.kind,
        "href": f"{file_url}?inline=1" if inline else thumb_url,
        "lightbox": blob.kind != "pdf",
        "thumb": thumb_url,
        "status": status_url,
        "title": title,
        "alt": alt,
        "lazy": lazy,
    }


@register.inclusion_tag("files/_attachments.html", takes_context=True)
def attachments(context, obj, title="Files"):
    request = context["request"]
    perm = services.target_permission(obj)
    can_see = access.has_perm(request.actor, "files.view")
    can_upload = perm is not None and access.has_perm(request.actor, "files.upload")
    return {
        "request": request,
        "csrf_token": context.get("csrf_token"),
        "title": title,
        "target": obj,
        "label": obj._meta.label_lower,
        "links": services.attachments_for(obj) if can_see else [],
        "can_see": can_see,
        "can_upload": can_upload,
        "types": DocumentType.objects.filter(is_active=True) if can_upload else [],
        "here": request.get_full_path(),
    }
