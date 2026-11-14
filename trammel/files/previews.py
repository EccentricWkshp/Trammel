"""Previews of stored files (FR-DOC-4): a picture of what's in them, made in the
background and shown as the file's thumbnail.

* Images and PDFs: the image, or the PDF's first page.
* DXF drawings: drawn as a vector SVG with ezdxf, black lines on white.
* STL and 3MF models: a shaded picture of the mesh, seen from the front, right
  and above; a 3MF that carries its own thumbnail shows that instead. A model
  also gets its measurements and a compact mesh for the 3D viewer (`make`,
  meshes.py, 0.11.0).
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
from dataclasses import dataclass
from pathlib import Path

#: Thumbnails of images and PDFs, longest side in pixels.
THUMB_PX = 480
#: Rendered models: big enough to open as the file's preview.
MODEL_PX = 960
#: The kinds that get a preview.
KINDS = frozenset({"image", "pdf", "dxf", "stl", "3mf", "step", "lightburn", "gcode"})
#: The kinds that are 3D models, opened in the viewer (0.11.0).
MODEL_KINDS = frozenset({"stl", "3mf", "step"})

#: What making one preview may use, in a child process (core.limits, 0.10.6):
#: room for a 2-million-triangle model (meshes.MAX_TRIANGLES), and time to
#: measure it, simplify it for the viewer and draw it.
MEMORY_MB = 1536
SECONDS = 120

#: Limits on what a reader takes in.
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
    if kind in MODEL_KINDS:
        made = make(kind, path)
        return made.picture if made else None
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
# Models: STL and 3MF (read, measured and packed for the viewer in meshes.py)
# ---------------------------------------------------------------------------


@dataclass
class Made:
    """What making a file's preview produced: its picture, and for a 3D model
    the viewer's mesh and the model's measurements (meshes.prepare)."""

    picture: tuple[bytes, str] | None
    mesh: bytes | None = None
    model: dict | None = None
    #: A sheet metal part's flat pattern (unfold.py), gzipped JSON.
    flat: bytes | None = None


def make(kind: str, path: Path) -> Made | None:
    """A file's preview, and for a model what the 3D viewer needs."""
    if kind not in MODEL_KINDS:
        picture = render(kind, path)
        return Made(picture) if picture else None
    from . import meshes

    picture = None
    if kind == "3mf":
        with zipfile.ZipFile(path) as zf:
            picture = _3mf_thumbnail(zf)
    try:
        model = meshes.read(kind, path)
        prepared = meshes.prepare(model) if model is not None else None
    except Exception:
        # A 3MF whose thumbnail is all that can be read still shows that.
        if picture is None:
            raise
        return Made(picture)
    if prepared is None:
        return Made(picture) if picture else None
    info = prepared.info
    facing = (-1 if info["inverted"] else 1) if info["solid"] else 0
    flat = None
    if model.flat:
        import gzip
        import json

        # A sheet metal part: its thickness and bends on the file, for the card.
        info["sheet"] = {
            "thickness": model.flat["thickness"],
            "bends": len(model.flat["bends"]),
        }
        flat = gzip.compress(json.dumps(model.flat).encode(), mtime=0)
    return Made(picture or _model(prepared.shown, facing), prepared.mesh, info, flat)


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


#: Triangles longer than this share of the model are split before drawing,
#: and splitting stops once there are this many.
SPLIT_SHARE = 1 / 24
SPLIT_LIMIT = 400_000


def _split_long(triangles):
    """Long triangles split in four, again until none is long. The picture is
    drawn back to front by each triangle's middle; a plane made of a few
    large triangles (as STEP files are) would otherwise sort wrongly against
    the narrow walls beside it, and show them through it."""
    import numpy as np

    flat = triangles.reshape(-1, 3)
    longest = max(float((flat.max(axis=0) - flat.min(axis=0)).max()), 1e-9) * SPLIT_SHARE
    for _ in range(8):
        sides = np.linalg.norm(triangles - triangles[:, [1, 2, 0]], axis=2).max(axis=1)
        long = sides > longest
        if not long.any() or len(triangles) + 3 * int(long.sum()) > SPLIT_LIMIT:
            break
        a, b, c = (triangles[long][:, i] for i in range(3))
        ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2
        parts = np.stack(
            [
                np.stack([a, ab, ca], 1),
                np.stack([ab, b, bc], 1),
                np.stack([ca, bc, c], 1),
                np.stack([ab, bc, ca], 1),
            ]
        ).reshape(-1, 3, 3)
        triangles = np.concatenate([triangles[~long], parts])
    return triangles


def _model(triangles, facing: int = 0) -> tuple[bytes, str] | None:
    """A shaded picture of a mesh: orthographic, from the front, right and above
    (Z up), lit from the upper left, drawn back to front. `facing` is 1 for a
    closed solid wound outward, -1 for one wound inward (meshes.prepare's
    "inverted"), so faces turned away are left out; 0 draws every face."""
    import numpy as np
    from PIL import Image, ImageDraw

    if triangles is None or not len(triangles):
        return None
    triangles = triangles[np.isfinite(triangles).all(axis=(1, 2))]
    if not len(triangles):
        return None
    eye = np.array([1.0, -1.4, 1.1])
    forward = -eye / np.linalg.norm(eye)
    if facing:
        # A closed solid's faces turned away can't be seen: leave them out,
        # so nothing behind is drawn over what's in front.
        outward = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
        triangles = triangles[(outward @ forward) * facing < 0]
        if not len(triangles):
            return None
    triangles = _split_long(triangles)
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
