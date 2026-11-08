"""File type from the bytes, not the client's claim (FR-DOC-5).

Binary formats are recognized by signature. Text formats (DXF, SVG, LightBurn,
G-code, STEP) by their content; the filename extension only breaks ties between
text formats that have no signature (G-code versus plain text).
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass


@dataclass(frozen=True)
class Detected:
    mime: str
    kind: str  # image, pdf, svg, dxf, lightburn, stl, 3mf, gcode, step, archive, text, other


def detect(head: bytes, name: str = "", size: int | None = None, path=None) -> Detected:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if head.startswith(b"\xff\xd8\xff"):
        return Detected("image/jpeg", "image")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return Detected("image/png", "image")
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return Detected("image/gif", "image")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return Detected("image/webp", "image")
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1"):
        return Detected("image/heic", "image")
    if head.startswith(b"%PDF-"):
        return Detected("application/pdf", "pdf")
    if head.startswith(b"AutoCAD Binary DXF"):
        return Detected("image/vnd.dxf", "dxf")
    if head.startswith(b"PK\x03\x04"):
        if path is not None and _zip_has(path, "3D/3dmodel.model"):
            return Detected("model/3mf", "3mf")
        return Detected("application/zip", "archive")
    if size is not None and size >= 84 and not head[:5].lower().startswith(b"solid"):
        count = int.from_bytes(head[80:84], "little") if len(head) >= 84 else -1
        if count >= 0 and 84 + count * 50 == size:
            return Detected("model/stl", "stl")

    text = _as_text(head)
    if text is None:
        return Detected("application/octet-stream", "other")
    stripped = text.lstrip("﻿ \t\r\n")
    lower = stripped[:2000].lower()
    if "<lightburnproject" in lower:
        return Detected("application/x-lightburn", "lightburn")
    if "<svg" in lower and (
        lower.startswith("<?xml")
        or lower.startswith("<svg")
        or lower.startswith("<!--")
        or lower.startswith("<!doctype svg")
    ):
        return Detected("image/svg+xml", "svg")
    if lower.startswith("solid") and "facet" in lower:
        return Detected("model/stl", "stl")
    first = [line.strip() for line in stripped.splitlines()[:4]]
    if len(first) >= 2 and first[0] == "0" and first[1] in ("SECTION", "section"):
        return Detected("image/vnd.dxf", "dxf")
    if stripped.startswith("ISO-10303-21"):
        return Detected("model/step", "step")
    if ext in ("gcode", "gco", "nc", "ngc", "tap") or _looks_like_gcode(stripped):
        return Detected("text/x-gcode", "gcode")
    return Detected("text/plain", "text")


def _as_text(head: bytes) -> str | None:
    if b"\x00" in head[:4096]:
        return None
    try:
        return head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character cut off by the sample boundary is still text.
        if exc.start > len(head) - 4:
            return head[: exc.start].decode("utf-8")
        try:
            return head.decode("latin-1")
        except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes anything
            return None


def _looks_like_gcode(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines()[:40] if line.strip()]
    commands = [line for line in lines if line[:1] in ("G", "M") and line[1:2].isdigit()]
    return len(lines) >= 3 and len(commands) >= len(lines) // 2


def _zip_has(path, member: str) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return member in zf.namelist()
    except zipfile.BadZipFile, OSError:
        return False


def strip_jpeg_gps(data: bytes) -> bytes:
    """Remove GPS tags from a JPEG's EXIF segment without re-encoding the image.

    The pixels and every other segment are kept byte-for-byte; only the APP1
    EXIF segment is replaced. Returns `data` unchanged when there is no GPS.
    """
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            exif = img.getexif()
    except Exception:  # noqa: BLE001 — an unreadable image is stored as-is
        return data
    gps_tag = 0x8825
    if gps_tag not in exif:
        return data
    del exif[gps_tag]
    new_payload = b"Exif\x00\x00" + exif.tobytes()

    out = bytearray(data[:2])  # SOI
    i = 2
    replaced = False
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:  # start of scan: the rest is image data
            break
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        segment = data[i : i + 2 + length]
        if not replaced and marker == 0xE1 and segment[4:10] == b"Exif\x00\x00":
            out += b"\xff\xe1" + (len(new_payload) + 2).to_bytes(2, "big") + new_payload
            replaced = True
        else:
            out += segment
        i += 2 + length
    out += data[i:]
    return bytes(out) if replaced else data
