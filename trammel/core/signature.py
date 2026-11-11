"""Drawn signatures (FR-AGR-7): what a signature pad sends, checked and cleaned.

The pad sends a PNG as a data: URL. It is decoded, checked to be a real,
reasonably sized image with something drawn on it, cropped to the ink and
re-encoded, so nothing but the drawing is ever stored.
"""

from __future__ import annotations

import base64
import binascii
import io

from django.core.exceptions import ValidationError

PREFIX = "data:image/png;base64,"
MAX_UPLOAD = 400_000  # bytes, before decoding is refused
MAX_SIDE = 4000  # pixels
STORED_WIDTH = 600  # pixels: plenty for a page, small in the database


def clean(data_url: str) -> bytes:
    """The signature as a cropped PNG, or ValidationError if there isn't one."""
    from PIL import Image, UnidentifiedImageError

    data_url = (data_url or "").strip()
    if not data_url:
        raise ValidationError("Sign in the box.")
    if not data_url.startswith(PREFIX) or len(data_url) > MAX_UPLOAD:
        raise ValidationError("The signature couldn't be read. Clear it and sign again.")
    try:
        raw = base64.b64decode(data_url[len(PREFIX) :], validate=True)
        image = Image.open(io.BytesIO(raw))
        if image.format != "PNG" or max(image.size) > MAX_SIDE:
            raise ValueError
        image = image.convert("RGBA")
    except binascii.Error, ValueError, OSError, UnidentifiedImageError:
        raise ValidationError("The signature couldn't be read. Clear it and sign again.") from None
    box = image.getchannel("A").getbbox()
    if box is None:
        raise ValidationError("Sign in the box.")
    left, top, right, bottom = box
    pad = 8
    image = image.crop(
        (
            max(left - pad, 0),
            max(top - pad, 0),
            min(right + pad, image.width),
            min(bottom + pad, image.height),
        )
    )
    if image.width > STORED_WIDTH:
        image = image.resize(
            (STORED_WIDTH, max(1, round(image.height * STORED_WIDTH / image.width)))
        )
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


def data_uri(png: bytes) -> str:
    """For showing a stored signature in a page or PDF."""
    if not png:
        return ""
    return "data:image/png;base64," + base64.b64encode(bytes(png)).decode()
