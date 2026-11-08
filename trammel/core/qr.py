"""QR codes as inline SVG (FR-WO-4, FR-STK-7, MFA setup). No raster dependency."""

from __future__ import annotations

import io


def svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()
