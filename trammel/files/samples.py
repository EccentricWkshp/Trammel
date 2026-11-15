"""3D models made from numbers, for the demo data (seed_demo) and the tests:
a turned tap handle as an STL, and sheet metal parts as STEP (with
OpenCascade). Each is a real, closed solid, so it previews, measures and (the
sheet metal) unfolds like a customer's file.
"""

from __future__ import annotations

import math
import struct

#: A tap handle's profile, turned about Z: (radius, height) in mm, from the
#: middle of its base to the middle of its top.
TAP_HANDLE = [
    (0.0, 0.0), (11.0, 0.0), (11.0, 16.0), (9.0, 18.0), (9.0, 22.0), (10.5, 26.0),
    (12.5, 60.0), (15.0, 120.0), (16.5, 150.0), (16.0, 165.0), (13.5, 176.0),
    (9.0, 183.0), (0.0, 185.0),
]  # fmt: skip


def turned_stl(profile=TAP_HANDLE, around: int = 96) -> bytes:
    """A binary STL of `profile` turned a full circle about Z, closed at both
    ends (the profile starts and ends on the axis), faces outward."""
    rings = []
    for r, z in profile:
        rings.append(
            [
                (r * math.cos(2 * math.pi * i / around), r * math.sin(2 * math.pi * i / around), z)
                for i in range(around)
            ]
        )
    triangles = []
    for lower, upper in zip(rings, rings[1:], strict=False):
        for i in range(around):
            a, b = lower[i], lower[(i + 1) % around]
            c, d = upper[(i + 1) % around], upper[i]
            # Seen from outside, counterclockwise; none where a ring is the axis.
            if a != b:
                triangles.append((a, b, c))
            if c != d:
                triangles.append((a, c, d))
    out = bytearray(b"Trammel demo: tap handle".ljust(80, b"\0"))
    out += struct.pack("<I", len(triangles))
    for tri in triangles:
        out += struct.pack("<3f", 0, 0, 0)
        for corner in tri:
            out += struct.pack("<3f", *corner)
        out += b"\0\0"
    return bytes(out)


# ---------------------------------------------------------------------------
# Sheet metal, as STEP (needs OpenCascade)


def step_bytes(shape, unit: str = "MM") -> bytes:
    """A shape written as a STEP file, in `unit` ("MM" or "INCH")."""
    import tempfile
    from pathlib import Path

    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    # After the writer exists: making the first one in a process sets
    # OpenCascade's STEP defaults, the unit among them.
    writer = STEPControl_Writer()
    Interface_Static.SetCVal_s("write.step.unit", unit)
    try:
        writer.Transfer(shape, STEPControl_AsIs)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "part.step"
            writer.Write(str(path))
            return path.read_bytes()
    finally:
        Interface_Static.SetCVal_s("write.step.unit", "MM")


def bracket(
    first=60.0,
    second=40.0,
    radius=3.0,
    thickness=4.7625,
    width=50.0,
    hole=10.0,
    holes=(),
    unit="MM",
) -> bytes:
    """A sheet metal L-bracket as a STEP file: a flat flange `first` mm long
    (to where the bend starts), a 90° bend of inside `radius`, and a flange
    `second` mm long, `width` wide and `thickness` thick. Laid flat it's
    first + second + π/2 × (radius + K × thickness) long.

    `hole` is a hole of that diameter in the middle of the first flange (0 for
    none); `holes` adds more, as (flange 1 or 2, distance from the bend's
    start along the flange, distance across, diameter)."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakeWire,
    )
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt, gp_Vec

    r, t = radius, thickness
    # The section in the XZ plane: the first flange along -X, the bend about
    # an axis along Y at (0, r), the second flange up +Z.
    inner_arc = GC_MakeArcOfCircle(
        gp_Circ(gp_Ax2(gp_Pnt(0, 0, r), gp_Dir(0, -1, 0)), r),
        gp_Pnt(0, 0, 0),
        gp_Pnt(r, 0, r),
        True,
    ).Value()
    outer_arc = GC_MakeArcOfCircle(
        gp_Circ(gp_Ax2(gp_Pnt(0, 0, r), gp_Dir(0, -1, 0)), r + t),
        gp_Pnt(0, 0, -t),
        gp_Pnt(r + t, 0, r),
        True,
    ).Value()
    p = gp_Pnt
    edges = [
        BRepBuilderAPI_MakeEdge(p(-first, 0, 0), p(0, 0, 0)).Edge(),
        BRepBuilderAPI_MakeEdge(inner_arc).Edge(),
        BRepBuilderAPI_MakeEdge(p(r, 0, r), p(r, 0, r + second)).Edge(),
        BRepBuilderAPI_MakeEdge(p(r, 0, r + second), p(r + t, 0, r + second)).Edge(),
        BRepBuilderAPI_MakeEdge(p(r + t, 0, r + second), p(r + t, 0, r)).Edge(),
        BRepBuilderAPI_MakeEdge(outer_arc).Edge(),
        BRepBuilderAPI_MakeEdge(p(0, 0, -t), p(-first, 0, -t)).Edge(),
        BRepBuilderAPI_MakeEdge(p(-first, 0, -t), p(-first, 0, 0)).Edge(),
    ]
    wire = BRepBuilderAPI_MakeWire()
    for edge in edges:
        wire.Add(edge)
    face = BRepBuilderAPI_MakeFace(wire.Wire()).Face()
    shape = BRepPrimAPI_MakePrism(face, gp_Vec(0, width, 0)).Shape()
    cuts = list(holes)
    if hole:
        cuts.append((1, first / 2, width / 2, hole))
    for flange, along, across, diameter in cuts:
        if flange == 1:
            axis = gp_Ax2(gp_Pnt(-along, across, -t - 1), gp_Dir(0, 0, 1))
        else:
            axis = gp_Ax2(gp_Pnt(r - 1, across, r + along), gp_Dir(1, 0, 0))
        drill = BRepPrimAPI_MakeCylinder(axis, diameter / 2, t + 2).Shape()
        shape = BRepAlgoAPI_Cut(shape, drill).Shape()
    return step_bytes(shape, unit)


def shelf_bracket() -> bytes:
    """A floating-shelf bracket in 3/16 in mild steel: an 8 in arm under the
    shelf and a 4 in plate against the wall, 1-1/2 in wide, bent at 1/8 in
    inside radius, with two holes in each, drawn in inches."""
    inch = 25.4
    return bracket(
        first=8 * inch,
        second=4 * inch,
        radius=0.125 * inch,
        thickness=0.187 * inch,
        width=1.5 * inch,
        hole=0,
        holes=[
            (1, 1.5 * inch, 0.75 * inch, 0.265 * inch),
            (1, 6.5 * inch, 0.75 * inch, 0.265 * inch),
            (2, 1.0 * inch, 0.75 * inch, 0.375 * inch),
            (2, 3.0 * inch, 0.75 * inch, 0.375 * inch),
        ],
        unit="INCH",
    )


def channel(base=60.0, side=30.0, radius=3.0, thickness=4.7625, width=50.0) -> bytes:
    """A sheet metal U-channel as a STEP file: a base `base` mm across (between
    where its bends start) with a side `side` mm high up from each end, bent 90°
    at inside `radius`. Laid flat it's base + 2 × side + 2 × π/2 × (radius +
    K × thickness) long, its bend lines base + π/2 × (radius + K × thickness)
    apart."""
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeEdge,
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakeWire,
    )
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Pnt, gp_Vec

    r, t, R = radius, thickness, radius + thickness
    c, s = math.cos(math.radians(45)), math.sin(math.radians(45))

    def p(x, z):
        return gp_Pnt(x, 0, z)

    def line(a, b):
        return BRepBuilderAPI_MakeEdge(p(*a), p(*b)).Edge()

    def arc(a, m, b):
        return BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(p(*a), p(*m), p(*b)).Value()).Edge()

    # The section in the XZ plane: the inside of the base along z = 0, the
    # left bend about (0, r), the right about (base, r).
    edges = [
        arc((0, 0), (-r * c, r - r * s), (-r, r)),
        line((-r, r), (-r, r + side)),
        line((-r, r + side), (-R, r + side)),
        line((-R, r + side), (-R, r)),
        arc((-R, r), (-R * c, r - R * s), (0, -t)),
        line((0, -t), (base, -t)),
        arc((base, -t), (base + R * c, r - R * s), (base + R, r)),
        line((base + R, r), (base + R, r + side)),
        line((base + R, r + side), (base + r, r + side)),
        line((base + r, r + side), (base + r, r)),
        arc((base + r, r), (base + r * c, r - r * s), (base, 0)),
        line((base, 0), (0, 0)),
    ]
    wire = BRepBuilderAPI_MakeWire()
    for edge in edges:
        wire.Add(edge)
    face = BRepBuilderAPI_MakeFace(wire.Wire()).Face()
    return step_bytes(BRepPrimAPI_MakePrism(face, gp_Vec(0, width, 0)).Shape())


def block(size=(50.0, 50.0, 40.0)) -> bytes:
    """A solid block, as STEP: not sheet metal."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return step_bytes(BRepPrimAPI_MakeBox(*size).Shape())
