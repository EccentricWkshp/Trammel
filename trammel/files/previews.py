"""Previews of stored files (FR-DOC-4): a picture of what's in them, made in the
background and shown as the file's thumbnail.

* Images and PDFs: the image, or the PDF's first page.
* DXF drawings: drawn as a vector SVG with ezdxf, black lines on white.
* STL and 3MF models: a shaded picture of the mesh, seen from the front, right
  and above; a 3MF that carries its own thumbnail shows that instead.
* LightBurn projects and G-code: the thumbnail the program saved in the file,
  when there is one; no preview otherwise.

`render` returns (bytes, extension) or None when the file has nothing to show.
Every reader caps what it will take in, so a hostile file can't exhaust memory.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
import zipfile
from pathlib import Path

#: Thumbnails of images and PDFs, longest side in pixels.
THUMB_PX = 480
#: Rendered models: big enough to open as the file's preview.
MODEL_PX = 960
#: The kinds that get a preview.
KINDS = frozenset({"image", "pdf", "dxf", "stl", "3mf", "lightburn", "gcode"})

#: What making one preview may use, in a child process (core.limits, 0.10.6):
#: room for a 2-million-triangle model, and time to draw it.
MEMORY_MB = 1536
SECONDS = 120

#: Limits on what a reader takes in.
MAX_TRIANGLES = 2_000_000
MAX_3MF_MODEL_BYTES = 400 * 1024 * 1024
MAX_EMBEDDED_BYTES = 8 * 1024 * 1024
GCODE_SCAN_BYTES = 8 * 1024 * 1024
LIGHTBURN_SCAN_BYTES = 64 * 1024 * 1024


def render(kind: str, path: Path) -> tuple[bytes, str] | None:
    if kind == "image":
        from PIL import Image, ImageOps

        with Image.open(path) as image:
            return _webp(ImageOps.exif_transpose(image), THUMB_PX), "webp"
    if kind == "pdf":
        import pypdfium2

        pdf = pypdfium2.PdfDocument(str(path))
        try:
            page = pdf[0]
            scale = pdf_scale(*page.get_size())
            if scale is None:
                return None
            image = page.render(scale=scale).to_pil()
        finally:
            pdf.close()
        return _webp(image, THUMB_PX), "webp"
    if kind == "dxf":
        return _dxf(path)
    if kind == "stl":
        return _model(_stl(path))
    if kind == "3mf":
        return _3mf(path)
    if kind == "lightburn":
        return _embedded(_lightburn_thumbnail(path))
    if kind == "gcode":
        return _embedded(_gcode_thumbnail(path))
    return None


#: A PDF page is drawn no larger than this on its longest side, in pixels,
#: before it's shrunk to a thumbnail (SEC-02, 0.10.5): letter and A4 pages
#: still at 1.5, and never more than about 7.7 MB of pixels.
PDF_RENDER_PX = 1600


def pdf_scale(width: float, height: float) -> float | None:
    """The scale to draw a PDF page at for its thumbnail, from its size in
    points: 1.5 for an ordinary page, less for a large one, so its size in
    bytes says nothing about the picture drawn (a 553-byte PDF can declare a
    page 14,000 points square). None for a page with no size."""
    longest = max(width or 0, height or 0)
    if not longest > 0:
        return None
    return min(1.5, PDF_RENDER_PX / longest)


def _webp(image, px: int) -> bytes:
    image.thumbnail((px, px))
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA")
    out = io.BytesIO()
    image.save(out, "WEBP", quality=80)
    return out.getvalue()


def _embedded(data: bytes | None) -> tuple[bytes, str] | None:
    """A picture a program saved inside the file, re-encoded so only pixels pass on."""
    if not data:
        return None
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return _webp(image.copy(), THUMB_PX), "webp"
    except Exception:  # noqa: BLE001 — a broken embedded picture is no preview
        return None


# ---------------------------------------------------------------------------
# DXF
# ---------------------------------------------------------------------------


def _dxf(path: Path) -> tuple[bytes, str] | None:
    import logging

    from ezdxf import recover

    # ezdxf notes every drawing feature it can't draw exactly; only its
    # warnings are worth a line in the log.
    logging.getLogger("ezdxf").setLevel(logging.WARNING)
    from ezdxf.addons.drawing import Frontend, RenderContext, config, layout, svg
    from ezdxf.bbox import extents

    doc, _auditor = recover.readfile(str(path))
    msp = doc.modelspace()
    if not extents(msp, fast=True).has_data:
        return None
    backend = svg.SVGBackend()
    settings = config.Configuration(
        background_policy=config.BackgroundPolicy.WHITE,
        color_policy=config.ColorPolicy.BLACK,
        lineweight_policy=config.LineweightPolicy.RELATIVE_FIXED,
    )
    Frontend(RenderContext(doc), backend, config=settings).draw_layout(msp)
    # Sized to the drawing, no bigger than a page.
    page = layout.Page(
        0, 0, layout.Units.mm, margins=layout.Margins.all(4), max_width=400, max_height=400
    )
    text = backend.get_string(page, settings=layout.Settings(fit_page=True))
    return text.encode(), "svg"


# ---------------------------------------------------------------------------
# Meshes: STL and 3MF
# ---------------------------------------------------------------------------

_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_VERTEX = re.compile(rf"vertex\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})".encode())


def _stl(path: Path):
    """The triangles of an STL file as an (n, 3, 3) array, binary or ASCII."""
    import numpy as np

    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(84)
        count = int.from_bytes(head[80:84], "little") if len(head) == 84 else -1
        if count >= 0 and 84 + count * 50 == size:
            count = min(count, MAX_TRIANGLES)
            record = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
            data = np.frombuffer(fh.read(count * 50), dtype=record, count=count)
            return data["v"].astype(np.float64)
    text = path.read_bytes()
    points = _VERTEX.findall(text, 0)[: MAX_TRIANGLES * 3]
    usable = len(points) - len(points) % 3
    if not usable:
        return np.zeros((0, 3, 3))
    return np.array(points[:usable], dtype=np.float64).reshape(-1, 3, 3)


def _3mf(path: Path) -> tuple[bytes, str] | None:
    with zipfile.ZipFile(path) as zf:
        thumb = _3mf_thumbnail(zf)
        if thumb is not None:
            return thumb
        return _model(_3mf_mesh(zf))


def _3mf_thumbnail(zf: zipfile.ZipFile) -> tuple[bytes, str] | None:
    names = {info.filename.lstrip("/"): info for info in zf.infolist()}
    target = None
    if "_rels/.rels" in names and names["_rels/.rels"].file_size < 1024 * 1024:
        rels = zf.read("_rels/.rels").decode("utf-8", "replace")
        match = re.search(r'Target="([^"]+)"[^>]*Type="[^"]*thumbnail"', rels) or re.search(
            r'Type="[^"]*thumbnail"[^>]*Target="([^"]+)"', rels
        )
        target = match.group(1).lstrip("/") if match else None
    for name in (target, "Metadata/thumbnail.png"):
        info = names.get(name or "")
        if info is not None and info.file_size <= MAX_EMBEDDED_BYTES:
            return _embedded(zf.read(info))
    return None


def _3mf_mesh(zf: zipfile.ZipFile):
    """Every mesh in the 3MF's models, as modeled (build transforms aren't applied)."""
    import xml.etree.ElementTree as ET

    import numpy as np

    triangles = []
    total = 0
    for info in zf.infolist():
        if not info.filename.lower().endswith(".model"):
            continue
        if info.file_size > MAX_3MF_MODEL_BYTES:
            continue
        with zf.open(info) as fh:
            prolog = fh.read(64 * 1024)
        # A 3MF model never declares a DOCTYPE; one that does could define
        # entities to expand, so it isn't read at all.
        root = re.search(rb"<[A-Za-z_]", prolog)
        if root is None or b"<!DOCTYPE" in prolog[: root.start()].upper():
            continue
        vertices: list[tuple[float, float, float]] = []
        with zf.open(info) as fh:
            for _event, el in ET.iterparse(fh, events=("end",)):  # noqa: S314 — no DTD, above
                tag = el.tag.rsplit("}", 1)[-1]
                if tag == "vertex":
                    vertices.append((float(el.get("x")), float(el.get("y")), float(el.get("z"))))
                elif tag == "triangle" and total < MAX_TRIANGLES:
                    try:
                        a, b, c = (int(el.get(k)) for k in ("v1", "v2", "v3"))
                        triangles.append((vertices[a], vertices[b], vertices[c]))
                        total += 1
                    except IndexError, TypeError, ValueError:
                        pass
                elif tag == "object":
                    vertices = []
                el.clear()
    if not triangles:
        return np.zeros((0, 3, 3))
    return np.array(triangles, dtype=np.float64)


def _model(triangles) -> tuple[bytes, str] | None:
    """A shaded picture of a mesh: orthographic, from the front, right and above
    (Z up), lit from the upper left, drawn back to front."""
    import numpy as np
    from PIL import Image, ImageDraw

    if triangles is None or not len(triangles):
        return None
    triangles = triangles[np.isfinite(triangles).all(axis=(1, 2))]
    if not len(triangles):
        return None
    eye = np.array([1.0, -1.4, 1.1])
    forward = -eye / np.linalg.norm(eye)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    view = np.stack([right, up, forward])  # rows: screen x, screen y, depth
    points = triangles @ view.T  # (n, 3, 3)

    normals = np.cross(points[:, 1] - points[:, 0], points[:, 2] - points[:, 0])
    length = np.linalg.norm(normals, axis=1)
    keep = length > 0
    points, normals, length = points[keep], normals[keep], length[keep]
    if not len(points):
        return None
    normals /= length[:, None]
    light = np.array([-0.45, 0.6, -0.65])
    light /= np.linalg.norm(light)
    # Both sides lit alike: many STL files have their normals the wrong way out.
    shade = 0.38 + 0.62 * np.abs(normals @ light)

    scale_up = 2  # drawn twice the size, then scaled down to smooth the edges
    px = MODEL_PX * scale_up
    margin = px * 0.06
    xy = points[:, :, :2]
    low, high = xy.reshape(-1, 2).min(axis=0), xy.reshape(-1, 2).max(axis=0)
    span = max(float((high - low).max()), 1e-9)
    factor = (px - 2 * margin) / span
    offset = (px - (high - low) * factor) / 2
    screen = (xy - low) * factor + offset
    screen[:, :, 1] = px - screen[:, :, 1]  # image y runs down

    order = np.argsort(-points[:, :, 2].mean(axis=1))  # farthest first
    base = np.array([96, 140, 182])
    colors = np.clip(base[None, :] * shade[:, None] + 28 * shade[:, None] ** 4, 0, 255)
    colors = colors.astype(np.uint8)

    image = Image.new("RGB", (px, px), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    coords = screen.tolist()
    fills = [tuple(c) for c in colors.tolist()]
    for i in order.tolist():
        tri = coords[i]
        color = fills[i]
        draw.polygon([tuple(tri[0]), tuple(tri[1]), tuple(tri[2])], fill=color, outline=color)
    image = image.resize((MODEL_PX, MODEL_PX), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "WEBP", quality=82)
    return out.getvalue(), "webp"


# ---------------------------------------------------------------------------
# Thumbnails saved in the file
# ---------------------------------------------------------------------------

_LIGHTBURN_THUMB = re.compile(rb'<Thumbnail\s+Source="([A-Za-z0-9+/=\s]+)"')


def _lightburn_thumbnail(path: Path) -> bytes | None:
    with path.open("rb") as fh:
        text = fh.read(LIGHTBURN_SCAN_BYTES)
    match = _LIGHTBURN_THUMB.search(text)
    return _b64(match.group(1)) if match else None


_GCODE_THUMB = re.compile(
    rb"^;\s*thumbnail(?:_(?:JPG|PNG))?\s+begin\s+(\d+)x(\d+)\s+\d+\s*$(.*?)^;\s*thumbnail(?:_(?:JPG|PNG))?\s+end",
    re.MULTILINE | re.DOTALL,
)


def _gcode_thumbnail(path: Path) -> bytes | None:
    """The largest PNG or JPEG thumbnail a slicer wrote into the G-code's comments
    (PrusaSlicer, OrcaSlicer, Bambu Studio, Cura with its thumbnail plugin)."""
    with path.open("rb") as fh:
        text = fh.read(GCODE_SCAN_BYTES)
    best, area = None, -1
    for match in _GCODE_THUMB.finditer(text):
        w, h = int(match.group(1)), int(match.group(2))
        body = b"".join(line.lstrip(b"; ").strip() for line in match.group(3).splitlines())
        if w * h > area and (data := _b64(body)):
            best, area = data, w * h
    return best


def _b64(text: bytes) -> bytes | None:
    if len(text) > MAX_EMBEDDED_BYTES * 4 // 3 + 16:
        return None
    try:
        return base64.b64decode(b"".join(text.split()), validate=True)
    except binascii.Error, ValueError:
        return None
