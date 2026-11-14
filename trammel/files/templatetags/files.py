import json

from django import template

from trammel.core import access

from .. import services
from ..models import DocumentType
from ..previews import MODEL_KINDS

register = template.Library()


@register.inclusion_tag("files/_preview.html", takes_context=True)
def file_preview(
    context,
    revision,
    file_url,
    thumb_url,
    status_url,
    title="",
    alt="",
    lazy=True,
    model="",
    flat="",
):
    """A file's preview on a card: its picture, opening over the page (a PDF in a
    new tab); while it is being made, a placeholder the page swaps for it when
    it's ready (previews.js); otherwise the kind of file. Given `model` (the
    file's files:model address), a 3D model opens in the viewer (viewer.js)
    instead of as a picture; the picture stays the fallback."""
    blob = revision.blob
    inline = blob.kind in ("image", "svg", "pdf")
    return {
        "model": model if blob.kind in MODEL_KINDS else "",
        "model_info": json.dumps(blob.model) if model and blob.model else "",
        # A sheet metal part: the viewer links to its flat pattern (0.11.2).
        "flat": flat if blob.flat else "",
        "units": "metric" if _metric(context) else "imperial",
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


def _metric(context) -> bool:
    business = context.get("business")
    if business is None and (request := context.get("request")) is not None:
        business = getattr(request, "business", None)
    return getattr(business, "unit_system", "") == "metric"


def _number(value: float, places: int) -> str:
    text = f"{value:,.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


@register.simple_tag(takes_context=True)
def model_size(context, blob) -> str:
    """A 3D model's size across X, Y and Z, in the shop's units: "4.094 × 4.094 ×
    0.945 in", or "104 × 104 × 24 mm"."""
    info = getattr(blob, "model", None) or {}
    size = info.get("size")
    if not size or len(size) != 3:
        return ""
    if _metric(context):
        return " × ".join(_number(v, 1) for v in size) + " mm"
    return " × ".join(_number(v / 25.4, 3) for v in size) + " in"


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
