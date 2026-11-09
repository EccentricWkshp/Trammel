"""The business logo (FR-ADM-3): checked, normalized, and embedded in documents.

The type comes from the bytes, never from the browser's claim. Raster images
are decoded and saved again, which proves they are images, drops metadata such
as GPS EXIF (FR-DOC-6), and scales anything oversized down to a sensible size.
SVG is kept as drawn, but refused if it carries script or embedded HTML.

Documents embed the logo as a data: URI. That works the same on screen and in
PDF output, where nothing outside the document is ever fetched.
"""

from __future__ import annotations

import base64
import io
import re

from django.core.exceptions import ValidationError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_SVG_BYTES = 512 * 1024
MAX_PIXELS = 1600  # longest side, after scaling down
RASTER_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
SVG_FORBIDDEN = ("<script", "javascript:", "<foreignobject")
# Event handler attributes such as onload= or onclick= inside any tag.
EVENT_HANDLER = re.compile(r"<[^>]*\son[a-z]+\s*=")


def prepare(upload) -> tuple[bytes, str]:
    """Check an uploaded logo and return (bytes, MIME type) ready to store."""
    from trammel.files.sniff import detect

    if upload.size > MAX_UPLOAD_BYTES:
        raise ValidationError("The logo is too large. Use an image under 5 MB.")
    data = upload.read()
    detected = detect(data[:8192], getattr(upload, "name", ""), len(data))
    if detected.mime == "image/svg+xml":
        return _svg(data), "image/svg+xml"
    if detected.mime in RASTER_TYPES:
        return _raster(data)
    raise ValidationError("Use a PNG, JPEG, WebP, GIF or SVG image for the logo.")


def _svg(data: bytes) -> bytes:
    if len(data) > MAX_SVG_BYTES:
        raise ValidationError("The SVG logo is too large. Use one under 512 KB.")
    text = data.decode("utf-8", errors="replace").lower()
    if any(marker in text for marker in SVG_FORBIDDEN) or EVENT_HANDLER.search(text):
        raise ValidationError("This SVG contains script, which a logo cannot have.")
    return data


def _raster(data: bytes) -> tuple[bytes, str]:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValidationError("The logo could not be read as an image.") from exc
    image.thumbnail((MAX_PIXELS, MAX_PIXELS))
    out = io.BytesIO()
    if image.format == "JPEG" and image.mode in ("RGB", "L"):
        image.save(out, "JPEG", quality=90, optimize=True)
        return out.getvalue(), "image/jpeg"
    # Everything else becomes PNG, keeping any transparency.
    if image.mode not in ("RGB", "RGBA", "L", "LA"):
        image = image.convert("RGBA")
    image.save(out, "PNG", optimize=True)
    return out.getvalue(), "image/png"


def data_uri(business) -> str:
    """The logo as a data: URI, or "" when there is none."""
    data = bytes(business.logo or b"")
    if not data or not business.logo_type:
        return ""
    return f"data:{business.logo_type};base64,{base64.b64encode(data).decode('ascii')}"
