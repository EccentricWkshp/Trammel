"""3D models (FR-DOC-4, 0.11.0): read STL and 3MF files (and STEP, through
step.py, 0.11.1) into triangles, measure them, and prepare the compact mesh the
browser's viewer draws (viewer.js).

Everything here runs in the limited child that makes a file's preview
(core.limits), and every reader caps what it takes in.

The viewer's mesh is gzipped, little-endian:

    8 bytes   b"TRMESH01"
    uint32    triangles, T
    uint32    edge segments, E
    float32×3 the model's lowest corner, in millimeters
    float32×3 its size, in millimeters
    uint16    T×9 corner coordinates, each 0..65535 across the model's size
    uint16    E×6 edge segment ends, the same way

Triangles aren't indexed (the viewer shades each flat, so corners aren't
shared); edges are the model's sharp edges and open boundaries, drawn when the
viewer shows edges.
"""

from __future__ import annotations

import gzip
import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

#: Triangles read from one file, at most; a file with more is shown in part.
MAX_TRIANGLES = 2_000_000
MAX_3MF_MODEL_BYTES = 400 * 1024 * 1024
#: 3MF components inside components, at most this deep.
MAX_DEPTH = 8
#: The viewer is sent at most this many triangles; a model with more is
#: simplified for it (its measurements are of the whole model).
VIEW_TRIANGLES = 750_000
#: Edge segments sent to the viewer, at most.
MAX_EDGES = 400_000
#: Faces meeting at more than this many degrees make a sharp edge.
SHARP_DEGREES = 30.0

#: Millimeters in each unit a 3MF model may be in.
UNITS_MM = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}

MAGIC = b"TRMESH01"
HEADER = struct.Struct("<8sII3f3f")


@dataclass
class Model:
    #: (n, 3, 3) corners, in millimeters, Z up.
    triangles: object
    #: The unit the file says it's in (3MF), or "" when it says none (STL).
    unit: str
    #: Parts placed (a 3MF's build items; a STEP file's solids; 1 for an STL).
    objects: int
    #: Whether the file had more triangles than are read.
    truncated: bool
    #: The model's own edges, (n, 2, 3) in millimeters, when the file has them
    #: (STEP); otherwise they're found from the angles between faces.
    edges: object = None
    #: Exact measurements from the model's geometry (STEP): {"volume", "area"}
    #: in mm³ and mm², and "solid" when it's made of closed solids.
    exact: dict | None = None


def read(kind: str, path: Path) -> Model | None:
    if kind == "stl":
        return _stl(path)
    if kind == "3mf":
        with zipfile.ZipFile(path) as zf:
            return read_3mf(zf)
    if kind == "step":
        from . import step

        return step.read(path)
    return None


# ---------------------------------------------------------------------------
# STL
# ---------------------------------------------------------------------------

_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_VERTEX = re.compile(rf"vertex\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})".encode())


def _stl(path: Path) -> Model:
    """An STL file's triangles, binary or ASCII. STL names no unit; it's read as
    millimeters, as slicers do."""
    import numpy as np

    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(84)
        count = int.from_bytes(head[80:84], "little") if len(head) == 84 else -1
        if count >= 0 and 84 + count * 50 == size:
            take = min(count, MAX_TRIANGLES)
            record = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
            data = np.frombuffer(fh.read(take * 50), dtype=record, count=take)
            return Model(data["v"].astype(np.float64), "", 1, count > take)
    text = path.read_bytes()
    points = _VERTEX.findall(text, 0)
    truncated = len(points) > MAX_TRIANGLES * 3
    points = points[: MAX_TRIANGLES * 3]
    usable = len(points) - len(points) % 3
    if not usable:
        return Model(np.zeros((0, 3, 3)), "", 1, False)
    triangles = np.array(points[:usable], dtype=np.float64).reshape(-1, 3, 3)
    return Model(triangles, "", 1, truncated)


# ---------------------------------------------------------------------------
# 3MF
# ---------------------------------------------------------------------------


@dataclass
class _ModelFile:
    unit: str
    #: id -> ("mesh", vertices (v, 3), triangles (t, 3)) or ("components", [(path, id, matrix)])
    objects: dict
    #: [(path, id, matrix)]
    build: list


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(el, name: str) -> str | None:
    """An attribute by its local name, whatever namespace it's in (the
    production extension's p:path)."""
    for key, value in el.attrib.items():
        if _local(key) == name:
            return value
    return None


def _matrix(text: str | None):
    """A 3MF transform ("m00 m01 m02 m10 … m32", points as rows) as a 4×4
    matrix that multiplies row vectors; None when there's none or it's broken."""
    import numpy as np

    if not text:
        return None
    try:
        values = [float(v) for v in text.split()]
    except ValueError:
        return None
    if len(values) != 12 or not all(np.isfinite(values)):
        return None
    m = np.eye(4)
    m[:, :3] = np.array(values).reshape(4, 3)
    return m


def _root_name(zf: zipfile.ZipFile, names: dict) -> str:
    """The package's main model, named in _rels/.rels (3D/3dmodel.model as a rule)."""
    info = names.get("_rels/.rels")
    if info is not None and info.file_size < 1024 * 1024:
        rels = zf.read(info).decode("utf-8", "replace")
        for rel in re.findall(r"<Relationship\b[^>]*>", rels):
            kind = re.search(r'Type="([^"]*)"', rel)
            target = re.search(r'Target="([^"]*)"', rel)
            if kind and target and kind.group(1).endswith("/3dmodel"):
                return target.group(1).lstrip("/")
    return "3D/3dmodel.model"


def _parse(zf: zipfile.ZipFile, info, budget: list[int]) -> _ModelFile | None:
    """One model part. `budget` is the triangles still to be read, shared by
    every part, so a file can't hold more than MAX_TRIANGLES in all."""
    import xml.etree.ElementTree as ET

    import numpy as np

    if info.file_size > MAX_3MF_MODEL_BYTES:
        return None
    with zf.open(info) as fh:
        prolog = fh.read(64 * 1024)
    # A 3MF model never declares a DOCTYPE; one that does could define
    # entities to expand, so it isn't read at all.
    root = re.search(rb"<[A-Za-z_]", prolog)
    if root is None or b"<!DOCTYPE" in prolog[: root.start()].upper():
        return None

    unit = "millimeter"
    objects: dict = {}
    build: list = []
    vertices: list = []
    triangles: list = []
    components: list = []
    holder = None  # the element whose children are being read
    with zf.open(info) as fh:
        for event, el in ET.iterparse(fh, events=("start", "end")):  # noqa: S314 — no DTD, above
            tag = _local(el.tag)
            if event == "start":
                if tag == "model":
                    unit = (el.get("unit") or "millimeter").lower()
                elif tag == "object":
                    vertices, triangles, components = [], [], []
                elif tag in ("vertices", "triangles", "components", "build", "resources"):
                    holder = el
                continue
            if tag == "vertex":
                try:
                    vertices.append((float(el.get("x")), float(el.get("y")), float(el.get("z"))))
                except TypeError, ValueError:
                    vertices.append((np.nan, np.nan, np.nan))
            elif tag == "triangle":
                if budget[0] > 0:
                    try:
                        triangles.append((int(el.get("v1")), int(el.get("v2")), int(el.get("v3"))))
                        budget[0] -= 1
                    except TypeError, ValueError:
                        pass
                else:
                    budget[1] = 1  # more than was read
            elif tag == "component":
                components.append(
                    (_attr(el, "path"), el.get("objectid"), _matrix(el.get("transform")))
                )
            elif tag == "object":
                oid = el.get("objectid") or el.get("id")
                if triangles and vertices:
                    v = np.array(vertices, dtype=np.float64)
                    t = np.array(triangles, dtype=np.int64)
                    t = t[((t >= 0) & (t < len(v))).all(axis=1)]
                    objects[oid] = ("mesh", v, t)
                elif components:
                    objects[oid] = ("components", components)
                vertices, triangles, components = [], [], []
            elif tag == "item":
                build.append((_attr(el, "path"), el.get("objectid"), _matrix(el.get("transform"))))
            # What's been read is let go, so a large model isn't kept twice:
            # the element just read, and those before it in the same holder.
            el.clear()
            if holder is not None and holder is not el:
                holder.clear()
    return _ModelFile(unit, objects, build)


def read_3mf(zf: zipfile.ZipFile) -> Model:
    """The parts a 3MF file places on its build plate, each where its build item
    (and any components) put it."""
    import numpy as np

    names = {info.filename.lstrip("/"): info for info in zf.infolist()}
    root_name = _root_name(zf, names)
    budget = [MAX_TRIANGLES, 0]
    parts: dict[str, _ModelFile | None] = {}

    def part(name: str | None) -> _ModelFile | None:
        name = (name or root_name).lstrip("/")
        if name not in parts:
            info = names.get(name)
            parts[name] = _parse(zf, info, budget) if info is not None else None
        return parts[name]

    root = part(root_name)
    unit = root.unit if root is not None else "millimeter"
    scale = UNITS_MM.get(unit, 1.0)
    placed: list = []
    count = [0]

    def place(path, oid, matrix, depth: int) -> None:
        if depth > MAX_DEPTH or count[0] >= MAX_TRIANGLES:
            return
        model = part(path)
        obj = model.objects.get(oid) if model is not None else None
        if obj is None:
            return
        if obj[0] == "mesh":
            _, v, t = obj
            take = t[: MAX_TRIANGLES - count[0]]
            if not len(take):
                return
            v = v @ matrix[:3, :3] + matrix[3, :3]
            placed.append(v[take])
            count[0] += len(take)
            return
        for c_path, c_id, c_matrix in obj[1]:
            inner = c_matrix if c_matrix is not None else np.eye(4)
            # A component's own transform applies first, then its parent's.
            place(c_path or path or root_name, c_id, inner @ matrix, depth + 1)

    items = root.build if root is not None else []
    for path, oid, matrix in items:
        place(path or root_name, oid, matrix if matrix is not None else np.eye(4), 0)

    if not placed:
        # No build to follow: every mesh in every part, as modeled.
        for info in zf.infolist():
            if info.filename.lower().endswith(".model"):
                model = part(info.filename)
                for obj in model.objects.values() if model else ():
                    if obj[0] == "mesh" and count[0] < MAX_TRIANGLES:
                        take = obj[2][: MAX_TRIANGLES - count[0]]
                        placed.append(obj[1][take])
                        count[0] += len(take)
        items = [None] * len(placed)

    truncated = bool(budget[1]) or count[0] >= MAX_TRIANGLES
    if not placed:
        return Model(np.zeros((0, 3, 3)), unit, 0, truncated)
    triangles = np.concatenate(placed) * scale
    return Model(triangles, unit, len(items), truncated)


# ---------------------------------------------------------------------------
# Measuring, simplifying and packing for the viewer
# ---------------------------------------------------------------------------


@dataclass
class Prepared:
    #: What the viewer is sent, gzipped (see the module's docstring).
    mesh: bytes
    #: The triangles the viewer is sent, (n, 3, 3) in millimeters; the picture
    #: is drawn from these too.
    shown: object
    #: Measurements and counts, kept on the file (Blob.model).
    info: dict


def prepare(model: Model) -> Prepared | None:
    import numpy as np

    triangles = model.triangles
    triangles = triangles[np.isfinite(triangles).all(axis=(1, 2))]
    if not len(triangles):
        return None
    low = triangles.reshape(-1, 3).min(axis=0)
    high = triangles.reshape(-1, 3).max(axis=0)
    points, faces = _weld(triangles, low, high)
    faces = faces[
        (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    ]
    if not len(faces):
        return None

    a, b, c = (triangles[:, i] for i in range(3))
    cross = np.cross(b - a, c - a)
    area = 0.5 * float(np.linalg.norm(cross, axis=1).sum())
    signed = float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0
    closed, oriented, edges = _edges(points, faces)
    solid = closed and oriented
    volume = abs(signed)
    if model.edges is not None:
        # The file's own edges (STEP), as drawn in the CAD program.
        edges = model.edges[np.isfinite(model.edges).all(axis=(1, 2))][:MAX_EDGES]
    if model.exact:
        # Measured on the geometry itself, not the triangles.
        area = model.exact["area"]
        if model.exact.get("volume") is not None:
            volume = model.exact["volume"]
            solid = model.exact["solid"]

    shown_points, shown_faces = points, faces
    if len(faces) > VIEW_TRIANGLES:
        shown_points, shown_faces = _simplify(points, faces, low, high, VIEW_TRIANGLES)
    shown = shown_points[shown_faces]

    info = {
        "unit": model.unit,
        "size": [round(float(v), 4) for v in (high - low)],
        "area": round(area, 3),
        "volume": round(volume, 3),
        # A closed surface, every face the same way out (or a STEP file's
        # closed solids): its volume is exact.
        "solid": bool(solid),
        # Faces wound inward (normals pointing in): the viewer tells inside
        # from outside the other way round when drawing a section.
        "inverted": bool(signed < 0),
        "triangles": len(faces),
        "shown": len(shown_faces),
        "edges": len(edges),
        "objects": model.objects,
        "truncated": model.truncated,
    }
    return Prepared(_pack(shown, edges, low, high), shown, info)


def _weld(triangles, low, high):
    """Corners at the same place made one point: (points, faces)."""
    import numpy as np

    span = max(float((high - low).max()), 1e-9)
    steps = (1 << 21) - 2
    # The grid is shifted by an odd fraction of a step: a symmetric model puts
    # points exactly halfway between steps (its middle), where the 1e-16 a
    # sine leaves would round a seam's two sides apart.
    scaled = (triangles.reshape(-1, 3) - low) / span * steps + 0.3183
    q = np.clip(np.rint(scaled), 0, (1 << 21) - 1).astype(np.int64)
    keys = (q[:, 0] << 42) | (q[:, 1] << 21) | q[:, 2]
    _, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
    points = triangles.reshape(-1, 3)[first]
    return points, inverse.reshape(-1, 3)


def _edges(points, faces):
    """Whether the surface is closed (every edge shared by exactly two faces),
    whether its faces all wind the same way, and the segments the viewer draws
    as edges: sharp ones, open boundaries and edges shared by more than two."""
    import numpy as np

    n = len(points)
    start = faces.reshape(-1)
    end = faces[:, [1, 2, 0]].reshape(-1)
    face_of = np.repeat(np.arange(len(faces)), 3)
    low = np.minimum(start, end)
    high = np.maximum(start, end)
    undirected = low * n + high
    directed = start * n + end

    order = np.argsort(undirected, kind="stable")
    keys = undirected[order]
    bounds = np.flatnonzero(np.diff(keys)) + 1
    starts = np.concatenate([[0], bounds])
    counts = np.diff(np.concatenate([starts, [len(keys)]]))
    closed = bool((counts == 2).all())
    oriented = len(np.unique(directed)) == len(directed)

    normals = np.cross(
        points[faces[:, 1]] - points[faces[:, 0]], points[faces[:, 2]] - points[faces[:, 0]]
    )
    length = np.linalg.norm(normals, axis=1)
    normals /= np.where(length > 0, length, 1)[:, None]

    pairs = starts[counts == 2]
    f1 = face_of[order[pairs]]
    f2 = face_of[order[pairs + 1]]
    # Faces wound alike meet at the angle between their normals; one wound the
    # other way round (common in STL files) has its normal reversed.
    cos = np.einsum("ij,ij->i", normals[f1], normals[f2])
    cos = np.where(_wound_alike(start, end, order, pairs), cos, -cos)
    sharp = pairs[cos < np.cos(np.radians(SHARP_DEGREES))]
    odd = starts[counts != 2]
    chosen = np.concatenate([sharp, odd])[:MAX_EDGES]
    first = order[chosen]
    segments = np.stack([points[start[first]], points[end[first]]], axis=1)
    return closed, oriented, segments


def _wound_alike(start, end, order, pairs):
    """For each shared edge, whether its two faces run it in opposite directions
    (the way faces wound alike do)."""
    first = order[pairs]
    second = order[pairs + 1]
    return start[first] == end[second]


def _simplify(points, faces, low, high, target: int):
    """Fewer triangles for the viewer, by merging the points that share a cell of
    a grid over the model, finer or coarser until under `target`."""
    import numpy as np

    span = max(float((high - low).max()), 1e-9)
    cells = max(int(2 * np.sqrt(target)), 16)
    best = None
    for _ in range(6):
        size = span / cells
        q = np.floor((points - low) / size).astype(np.int64)
        np.clip(q, 0, (1 << 20) - 1, out=q)
        keys = (q[:, 0] << 42) | (q[:, 1] << 21) | q[:, 2]
        _, cluster = np.unique(keys, return_inverse=True)
        cluster = cluster.reshape(-1)
        merged = np.zeros((cluster.max() + 1, 3))
        np.add.at(merged, cluster, points)
        merged /= np.bincount(cluster)[:, None]
        f = cluster[faces]
        f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])]
        # The same triangle twice (either way round) is drawn once.
        key = np.sort(f, axis=1)
        _, keep = np.unique(key, axis=0, return_index=True)
        f = f[np.sort(keep)]
        best = (merged, f)
        if len(f) <= target:
            return best
        cells = max(int(cells * np.sqrt(target / len(f)) * 0.95), 4)
    merged, f = best
    return merged, f[:target]


def _quantize(values, low, size):
    import numpy as np

    scaled = (values - low) / np.where(size > 0, size, 1) * 65535
    return np.clip(np.rint(scaled), 0, 65535).astype("<u2")


def _pack(shown, edges, low, high) -> bytes:
    size = high - low
    header = HEADER.pack(MAGIC, len(shown), len(edges), *map(float, low), *map(float, size))
    body = _quantize(shown.reshape(-1, 3), low, size).tobytes()
    lines = _quantize(edges.reshape(-1, 3), low, size).tobytes() if len(edges) else b""
    return gzip.compress(header + body + lines, compresslevel=6, mtime=0)


def unpack(data: bytes):
    """The viewer's mesh read back: (triangles, edges, low, size), as numbers in
    millimeters. For tests; the browser reads it in viewer.js."""
    import numpy as np

    raw = gzip.decompress(data)
    magic, t, e, *rest = HEADER.unpack_from(raw)
    if magic != MAGIC:
        raise ValueError("not a Trammel mesh")
    low, size = np.array(rest[:3]), np.array(rest[3:])
    body = np.frombuffer(raw, dtype="<u2", count=t * 9, offset=HEADER.size)
    lines = np.frombuffer(raw, dtype="<u2", count=e * 6, offset=HEADER.size + t * 18)
    tri = body.reshape(-1, 3).astype(np.float64) / 65535 * size + low
    seg = lines.reshape(-1, 3).astype(np.float64) / 65535 * size + low
    return tri.reshape(-1, 3, 3), seg.reshape(-1, 2, 3), low, size
