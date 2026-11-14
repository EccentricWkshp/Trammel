"""STEP files (FR-DOC-4, 0.11.1), read with OpenCascade (cadquery-ocp-novtk).

A STEP file holds exact geometry (solids, faces, edges), not triangles. It's
read, cut into triangles fine enough to look smooth, and handed on as a Model
(meshes.py) with what only exact geometry gives: its own edges, as the CAD
program drew them, and its volume and surface area measured on the geometry
itself. OpenCascade gives millimeters whatever unit the file was drawn in;
the file's own unit is read from it for the viewer to name.

Runs in the limited child that makes previews (core.limits). Without
OpenCascade installed (a source install that left it out), STEP files simply
have no preview.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

#: How closely triangles follow curved faces: this share of the model's
#: diagonal, never finer than LINEAR_MIN mm; and at most this angle, in radians,
#: between neighbouring triangles on a curve.
LINEAR_SHARE = 0.0008
LINEAR_MIN = 0.005
ANGULAR = 0.3


def available() -> bool:
    try:
        import OCP  # noqa: F401
    except ImportError:
        return False
    return True


def read(path: Path):
    """The model in a STEP file, or None when OpenCascade isn't installed or the
    file holds no shape. Raises ValueError for a file it can't read."""
    if not available():
        log.info("No STEP preview: OpenCascade (cadquery-ocp-novtk) isn't installed.")
        return None
    from .meshes import Model

    shape = _shape(path)
    if shape is None:
        return None
    triangles, edges, truncated = _tessellate(shape)
    if triangles is None:
        return None
    exact = _measure(shape)
    return Model(
        triangles,
        file_unit(path),
        max(exact.pop("solids"), 1),
        truncated,
        edges=edges,
        exact=exact,
        flat=_unfold(shape, exact.get("volume")),
    )


def _unfold(shape, volume):
    """A sheet metal part's flat pattern (unfold.py, 0.11.2), or None. Never
    costs the file its preview: a part it can't unfold just has none."""
    from . import unfold

    try:
        return unfold.unfold(shape, volume)
    except Exception:
        log.warning("Couldn't unfold a STEP file", exc_info=True)
        return None


def _shape(path: Path):
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader

    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError("OpenCascade couldn't read this STEP file")
    if not reader.TransferRoots():
        return None
    shape = reader.OneShape()
    return None if shape.IsNull() else shape


def _tessellate(shape):
    """Triangles (n, 3, 3) and edge segments (n, 2, 3), in millimeters, and
    whether there were more triangles than meshes.MAX_TRIANGLES."""
    import numpy as np
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    from .meshes import MAX_EDGES, MAX_TRIANGLES

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return None, None, False
    low = np.array(box.CornerMin().Coord())
    high = np.array(box.CornerMax().Coord())
    diagonal = float(np.linalg.norm(high - low))
    # One thread: under the preview's memory limit, every thread's own heap
    # reservation counts against it.
    BRepMesh_IncrementalMesh(shape, max(diagonal * LINEAR_SHARE, LINEAR_MIN), False, ANGULAR, False)

    triangles, segments = [], []
    count = edge_count = 0
    truncated = False
    seen: dict[int, list] = {}
    faces = TopExp_Explorer(shape, TopAbs_FACE)
    while faces.More():
        face = TopoDS.Face(faces.Current())
        faces.Next()
        location = TopLoc_Location()
        mesh = BRep_Tool.Triangulation_s(face, location)
        if mesh is None:
            continue
        if count + mesh.NbTriangles() > MAX_TRIANGLES:
            truncated = True
            break
        move = location.Transformation()
        nodes = np.array(
            [mesh.Node(i).Transformed(move).Coord() for i in range(1, mesh.NbNodes() + 1)]
        )
        index = np.array([mesh.Triangle(i).Get() for i in range(1, mesh.NbTriangles() + 1)]) - 1
        if face.Orientation() == TopAbs_REVERSED:
            index = index[:, [0, 2, 1]]
        triangles.append(nodes[index])
        count += len(index)

        edges = TopExp_Explorer(face, TopAbs_EDGE)
        while edges.More() and edge_count < MAX_EDGES:
            edge = TopoDS.Edge(edges.Current())
            edges.Next()
            # Each edge once, though both its faces list it.
            same = seen.setdefault(hash(edge), [])
            if any(other.IsSame(edge) for other in same):
                continue
            same.append(edge)
            # A seam (where a cylinder's face meets itself) and a point
            # standing in for an edge (a cone's tip) aren't edges to draw.
            if BRep_Tool.Degenerated_s(edge) or BRep_Tool.IsClosed_s(edge, face):
                continue
            polygon = BRep_Tool.PolygonOnTriangulation_s(edge, mesh, location)
            if polygon is None:
                continue
            ids = [polygon.Node(i) - 1 for i in range(1, polygon.NbNodes() + 1)]
            points = nodes[ids]
            segments.append(np.stack([points[:-1], points[1:]], axis=1))
            edge_count += len(ids) - 1
    if not triangles:
        return None, None, truncated
    lines = np.concatenate(segments) if segments else np.zeros((0, 2, 3))
    return np.concatenate(triangles), lines, truncated


def _measure(shape) -> dict:
    """Volume and surface area on the geometry itself; whether it's made of
    closed solids (so the volume means something); how many solids."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    solids = 0
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    while explorer.More():
        solids += 1
        explorer.Next()
    surface = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, surface)
    measured = {"area": abs(surface.Mass()), "solids": solids}
    if solids:
        volume = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, volume)
        measured["volume"] = abs(volume.Mass())
        # Closed is a shell's property (a solid itself never says it is).
        closed = True
        shells = TopExp_Explorer(shape, TopAbs_SHELL)
        while shells.More():
            closed = closed and BRep_Tool.IsClosed_s(shells.Current())
            shells.Next()
        measured["solid"] = closed
    else:
        # Surfaces with no solid: its volume comes from the triangles, and
        # is marked approximate there.
        measured["volume"] = None
        measured["solid"] = False
    return measured


_INCH = re.compile(rb"CONVERSION_BASED_UNIT\s*\(\s*'INCH'", re.IGNORECASE)
_FOOT = re.compile(rb"CONVERSION_BASED_UNIT\s*\(\s*'FOOT'", re.IGNORECASE)
_SI_LENGTH = re.compile(rb"SI_UNIT\s*\(\s*(\.[A-Z]+\.|\$)\s*,\s*\.METRE\.\s*\)")
_SI_NAMES = {
    b".MILLI.": "millimeter",
    b".CENTI.": "centimeter",
    b".MICRO.": "micron",
    b"$": "meter",
}


def file_unit(path: Path) -> str:
    """The length unit the file was drawn in, in 3MF's names (meshes.UNITS_MM);
    millimeter when it doesn't say."""
    data = path.read_bytes()
    if _INCH.search(data):
        return "inch"
    if _FOOT.search(data):
        return "foot"
    match = _SI_LENGTH.search(data)
    if match:
        return _SI_NAMES.get(match.group(1), "millimeter")
    return "millimeter"
