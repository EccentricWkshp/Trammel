"""Unfolding sheet metal parts (FR-DOC-4, 0.11.2): a folded part in a STEP file
laid flat, for the laser (or plasma, or waterjet) and the press brake.

Runs with OpenCascade, in the limited child that makes previews (step.py), so
it has no database. The result doesn't depend on the K-factor: every point of
a flat pattern moves in a straight line as K changes (each bend's allowance,
angle × (inside radius + K × thickness), is linear in K, and the rotations
that lay each flange flat don't depend on it). So the pattern is kept at
K = 0 and as its change per unit of K, and flat.py lays it out for whatever K
the shop's material gives, without OpenCascade.

How it's unfolded:

* The part is planes and cylinders. Its thickness is the gap between the
  largest flat face and the nearest flat face parallel to it.
* One skin of the sheet is the largest flat face and every face reached from
  it across a smooth edge: flanges meet bends tangentially, while the sheet's
  edges (its outline and holes) meet the skin at a sharp angle. So the skin
  is one side of the sheet, and the edges it doesn't share are the cut.
* The largest face is laid flat as it is. Across each bend (a cylinder in the
  skin), the flange beyond is turned flat about the bend's axis and moved out
  by the bend's allowance; the bend itself is unrolled at the neutral radius.
* What's laid flat is checked: its area times the thickness, at K = 0.5 (the
  middle of the sheet), must match the solid's volume. A part that isn't
  sheet metal (a machined block, a casting) fails it and gets no flat pattern.

Not unfolded: bends running straight into bends (rolled or S-shaped parts),
a bend with no flange beyond it, and parts thicker than MAX_THICKNESS.
"""

from __future__ import annotations

import math
from collections import deque

#: Thicker than this (mm), it's not sheet metal to bend.
MAX_THICKNESS = 25.4
#: Faces this close to parallel, or edges this close to smooth (1 - |cos|).
PARALLEL = 1e-6
#: The flat area × thickness must match the volume this closely.
VOLUME_MATCH = 0.01
#: Points within this many mm are the same point (joining the cut into loops).
JOIN = 1e-3
#: Curves that aren't lines or circles are followed every this many degrees
#: of their own parameter, or at most this many points.
CURVE_POINTS = 96


class NotSheetMetal(Exception):
    pass


def unfold(shape, volume: float | None) -> dict | None:
    """The flat pattern of a sheet metal solid, or None when it isn't one."""
    try:
        return _Unfolder(shape).run(volume)
    except NotSheetMetal:
        return None


# ---------------------------------------------------------------------------


def _np():
    import numpy as np

    return np


class _Face:
    def __init__(self, shape):
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepGProp import BRepGProp
        from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane
        from OCP.GProp import GProp_GProps
        from OCP.TopAbs import TopAbs_REVERSED

        np = _np()
        self.shape = shape
        surface = BRepAdaptor_Surface(shape)
        kind = surface.GetType()
        self.kind = "plane" if kind == GeomAbs_Plane else "cyl" if kind == GeomAbs_Cylinder else ""
        flip = -1.0 if shape.Orientation() == TopAbs_REVERSED else 1.0
        if self.kind == "plane":
            axis = surface.Plane().Axis()
            # Pointing out of the material.
            self.normal = np.array(axis.Direction().Coord()) * flip
        elif self.kind == "cyl":
            cylinder = surface.Cylinder()
            axis = cylinder.Axis()
            self.origin = np.array(axis.Location().Coord())
            self.axis = np.array(axis.Direction().Coord())
            self.radius = cylinder.Radius()
            self.span = abs(surface.LastUParameter() - surface.FirstUParameter())
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(shape, props)
        self.area = props.Mass()
        self.centroid = np.array(props.CentreOfMass().Coord())
        self.edges: list[int] = []

    def normal_at(self, point):
        """The surface's direction at a point, either way out (for smoothness)."""
        if self.kind == "plane":
            return self.normal
        np = _np()
        radial = point - self.origin
        radial = radial - (radial @ self.axis) * self.axis
        return radial / np.linalg.norm(radial)


class _Unfolder:
    def __init__(self, shape):
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS

        self.faces: list[_Face] = []
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        while explorer.More():
            self.faces.append(_Face(TopoDS.Face(explorer.Current())))
            explorer.Next()
        # Each edge once, with the faces either side of it.
        self.edges: list[tuple] = []
        buckets: dict[int, list[int]] = {}
        for index, face in enumerate(self.faces):
            explorer = TopExp_Explorer(face.shape, TopAbs_EDGE)
            while explorer.More():
                edge = TopoDS.Edge(explorer.Current())
                explorer.Next()
                bucket = buckets.setdefault(hash(edge), [])
                for k in bucket:
                    if self.edges[k][0].IsSame(edge):
                        if index not in self.edges[k][1]:
                            self.edges[k][1].append(index)
                        face.edges.append(k)
                        break
                else:
                    bucket.append(len(self.edges))
                    face.edges.append(len(self.edges))
                    self.edges.append((edge, [index]))

    # -- the sheet ------------------------------------------------------------

    def run(self, volume):
        np = _np()
        planes = [i for i, f in enumerate(self.faces) if f.kind == "plane"]
        if not planes:
            raise NotSheetMetal
        self.base = max(planes, key=lambda i: self.faces[i].area)
        self.thickness = self._thickness(planes)
        self.skin = self._skin()
        if any(self.faces[i].kind not in ("plane", "cyl") for i in self.skin):
            raise NotSheetMetal

        at0 = self._lay_out(0.0)
        at1 = self._lay_out(1.0)
        loops, extents, bends = self._cut(at0, at1)

        data = {
            "version": 1,
            "thickness": round(self.thickness, 6),
            "loops": loops,
            "extents": extents,
            "bends": bends,
        }
        _square_up(data)
        if volume:
            from . import flat

            middle = flat.pattern(data, 0.5)
            if abs(middle.area * self.thickness / volume - 1) > VOLUME_MATCH:
                raise NotSheetMetal
            low, high = np.array(middle.low), np.array(middle.high)
            if self.thickness * 5 > float((high - low).min()):
                raise NotSheetMetal  # a block, not a sheet
        return data

    def _thickness(self, planes) -> float:
        base = self.faces[self.base]
        gaps = []
        for j in planes:
            other = self.faces[j]
            if j == self.base or abs(other.normal @ base.normal + 1) > PARALLEL:
                continue
            gap = float((base.centroid - other.centroid) @ base.normal)
            if gap > 1e-4:
                gaps.append(gap)
        if not gaps or min(gaps) > MAX_THICKNESS:
            raise NotSheetMetal
        return min(gaps)

    def _midpoint(self, k):
        return _points(self.edges[k][0], 3)[1]

    def _skin(self) -> set[int]:
        skin = {self.base}
        queue = deque([self.base])
        while queue:
            i = queue.popleft()
            for k in self.faces[i].edges:
                middle = None
                for j in self.edges[k][1]:
                    if j in skin:
                        continue
                    middle = self._midpoint(k) if middle is None else middle
                    a = self.faces[i].normal_at(middle)
                    b = self.faces[j].normal_at(middle)
                    if abs(abs(float(a @ b)) - 1) < PARALLEL:
                        skin.add(j)
                        queue.append(j)
        return skin

    # -- laying it out ----------------------------------------------------------

    def _lay_out(self, k_factor: float) -> dict:
        """Where each skin face goes in the flat for one K-factor: planes as a
        rigid map (rotation rows, shift) into z = 0; bends as their unroll."""
        np = _np()
        base = self.faces[self.base]
        x = None
        for k in base.edges:
            points = _points(self.edges[k][0])
            if len(points) == 2:
                x = points[1] - points[0]
                break
        if x is None:
            x = np.cross(base.normal, [1.0, 0, 0])
            if np.linalg.norm(x) < 0.5:
                x = np.cross(base.normal, [0, 1.0, 0])
        z = base.normal
        x = x - (x @ z) * z
        x /= np.linalg.norm(x)
        rows = np.stack([x, np.cross(z, x), z])
        planes = {self.base: (rows, -rows @ base.centroid)}
        bends: dict[int, dict] = {}
        queue = deque([self.base])
        while queue:
            p = queue.popleft()
            rows_p, shift_p = planes[p]
            plane = self.faces[p]
            for k in plane.edges:
                for c in self.edges[k][1]:
                    if c not in self.skin or c in bends or self.faces[c].kind != "cyl":
                        continue
                    bend = self._bend(p, k, c, k_factor)
                    bends[c] = bend
                    q = bend["far"]
                    if q in planes:
                        continue
                    # Close the bend (turn the far flange back about the axis),
                    # then slide it out along this flange by the allowance.
                    turn = _rotation(bend["axis"], -bend["phi"])
                    move = bend["origin"] - turn @ bend["origin"] + bend["out"] * bend["allowance"]
                    planes[q] = (rows_p @ turn, rows_p @ move + shift_p)
                    queue.append(q)
        missing = [i for i in self.skin if i not in planes and i not in bends]
        if missing:
            raise NotSheetMetal  # a bend into a bend, or a face it can't reach
        return {"planes": planes, "bends": bends}

    def _bend(self, p, k, c, k_factor) -> dict:
        np = _np()
        cylinder, plane = self.faces[c], self.faces[p]
        axis, origin = cylinder.axis, cylinder.origin
        start = self._midpoint(k)
        r0 = _radial(start, origin, axis)
        far = None
        for k2 in cylinder.edges:
            for q in self.edges[k2][1]:
                if q in (p, c) or q not in self.skin:
                    continue
                if self.faces[q].kind == "plane":
                    r1 = _radial(self._midpoint(k2), origin, axis)
                    far = (q, math.atan2(float(axis @ np.cross(r0, r1)), float(r0 @ r1)))
                else:
                    raise NotSheetMetal  # a bend running into a bend
        if far is None:
            raise NotSheetMetal  # nothing beyond the bend
        q, phi = far
        angle = abs(phi)
        if angle < 1e-6:
            raise NotSheetMetal
        inner = self._inner_radius(cylinder)
        allowance = angle * (inner + k_factor * self.thickness)
        # Into the bend along this flange's plane, away from the flange.
        out = start - plane.centroid
        out = out - (out @ axis) * axis - (out @ plane.normal) * plane.normal
        out /= np.linalg.norm(out)
        return {
            "plane": p,
            "far": q,
            "axis": axis,
            "origin": origin,
            "r0": r0,
            "radius": cylinder.radius,
            "inner": inner,
            "phi": phi,
            "angle": angle,
            "allowance": allowance,
            "out": out,
            # The skin on the inside of the bend: viewed from the skin, the
            # flange folds up, towards the viewer.
            "up": abs(cylinder.radius - inner) < 1e-4,
        }

    def _inner_radius(self, cylinder) -> float:
        np = _np()
        for other in self.faces:
            if other.kind != "cyl" or other is cylinder:
                continue
            if abs(abs(float(other.axis @ cylinder.axis)) - 1) > PARALLEL:
                continue
            offset = other.origin - cylinder.origin
            offset = offset - (offset @ cylinder.axis) * cylinder.axis
            if (
                np.linalg.norm(offset) < 1e-3
                and abs(abs(other.radius - cylinder.radius) - self.thickness) < 1e-3
            ):
                return min(other.radius, cylinder.radius)
        # No matching surface on the far skin: it's the inside if the far skin
        # would be further out.
        return cylinder.radius

    @staticmethod
    def _to_flat(layout, i, point):
        np = _np()
        if i in layout["planes"]:
            rows, shift = layout["planes"][i]
            return (rows @ point + shift)[:2]
        bend = layout["bends"][i]
        rows, shift = layout["planes"][bend["plane"]]
        along = (point - bend["origin"]) @ bend["axis"]
        radial = _radial(point, bend["origin"], bend["axis"])
        phi = math.atan2(
            float(bend["axis"] @ np.cross(bend["r0"], radial)), float(bend["r0"] @ radial)
        )
        share = phi / bend["phi"]
        base = bend["origin"] + along * bend["axis"] + bend["radius"] * bend["r0"]
        return (rows @ (base + bend["out"] * (share * bend["allowance"])) + shift)[:2]

    # -- the cut ------------------------------------------------------------------

    def _cut(self, at0, at1):
        """The cut as loops of entities, the bend extents, and each bend's line,
        each at K = 0 with its change per unit of K."""
        np = _np()
        profile, extents = [], []
        for i in sorted(self.skin):
            face = self.faces[i]
            for k in face.edges:
                edge, owners = self.edges[k]
                shared = [j for j in owners if j != i and j in self.skin]
                if shared:
                    # Between a flange and a bend: drawn once, from the bend.
                    if face.kind == "cyl" and self.faces[shared[0]].kind == "plane":
                        extents.append(self._entity(edge, i, at0, at1, straight=True))
                    continue
                profile.append(self._entity(edge, i, at0, at1, straight=face.kind == "cyl"))

        bends = []
        for c, bend0 in sorted(at0["bends"].items()):
            ends = []
            for k in self.faces[c].edges:
                ends += list(_points(self.edges[k][0]))
            along = [(p - bend0["origin"]) @ bend0["axis"] for p in ends]
            lo, hi = min(along), max(along)
            # The bend's line: down the middle of the bend, end to end.
            middle = _rotation(bend0["axis"], bend0["phi"] / 2) @ bend0["r0"]
            line0, line1 = [], []
            for s in (lo, hi):
                point = bend0["origin"] + s * bend0["axis"] + bend0["radius"] * middle
                line0.append(self._to_flat(at0, c, point))
                line1.append(self._to_flat(at1, c, point))
            bends.append(
                {
                    "angle": round(math.degrees(bend0["angle"]), 4),
                    "radius": round(bend0["inner"], 6),
                    "length": round(float(hi - lo), 6),
                    "up": bool(bend0["up"]),
                    "p": [list(map(float, v)) for v in line0],
                    "d": [list(map(float, b - a)) for a, b in zip(line0, line1, strict=True)],
                }
            )
        loops = _loops(profile)
        if not loops:
            raise NotSheetMetal
        # The outline is the loop around the most area; the rest are holes.
        areas = [abs(_loop_area(loop)) for loop in loops]
        outer = int(np.argmax(areas))
        return (
            [{"outer": n == outer, "items": loop} for n, loop in enumerate(loops)],
            extents,
            bends,
        )

    def _entity(self, edge, face, at0, at1, straight: bool) -> dict:
        """One edge in the flat: a line, an arc or circle (on a flange), or a
        polyline (anything else)."""
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Line

        np = _np()
        curve = BRepAdaptor_Curve(edge)
        kind = curve.GetType()
        t0, t1 = curve.FirstParameter(), curve.LastParameter()

        def both(point):
            a = self._to_flat(at0, face, point)
            b = self._to_flat(at1, face, point)
            return a, b - a

        if kind == GeomAbs_Line or (
            straight and kind == GeomAbs_Circle and self._crosses(face, edge)
        ):
            a, da = both(np.array(curve.Value(t0).Coord()))
            b, db = both(np.array(curve.Value(t1).Coord()))
            return _line(a, da, b, db)
        if kind == GeomAbs_Circle and self.faces[face].kind == "plane":
            circle = curve.Circle()
            center, dc = both(np.array(circle.Location().Coord()))
            radius = circle.Radius()
            if abs(abs(t1 - t0) - 2 * math.pi) < 1e-7:
                return {"kind": "circle", "c": _f(center), "dc": _f(dc), "r": radius}
            start, _ = both(np.array(curve.Value(t0).Coord()))
            end, _ = both(np.array(curve.Value(t1).Coord()))
            middle, _ = both(np.array(curve.Value((t0 + t1) / 2).Coord()))
            a0, a1, am = (math.atan2(*(p - center)[::-1]) for p in (start, end, middle))
            if (am - a0) % (2 * math.pi) > (a1 - a0) % (2 * math.pi):
                a0, a1 = a1, a0  # counterclockwise from a0 to a1, as DXF draws arcs
            return {
                "kind": "arc",
                "c": _f(center),
                "dc": _f(dc),
                "r": radius,
                "a0": math.degrees(a0),
                "a1": math.degrees(a1),
                "ends": [_f(start), _f(end)],
            }
        count = max(8, min(CURVE_POINTS, int(abs(t1 - t0) / math.radians(3)) + 2))
        pairs = [both(np.array(curve.Value(t).Coord())) for t in np.linspace(t0, t1, count)]
        return {"kind": "poly", "p": [_f(a) for a, _ in pairs], "d": [_f(d) for _, d in pairs]}

    def _crosses(self, face, edge) -> bool:
        """A circle on a bend's cylinder, square across it: flat, a line."""
        from OCP.BRepAdaptor import BRepAdaptor_Curve

        cylinder = self.faces[face]
        circle = BRepAdaptor_Curve(edge).Circle()
        direction = _np().array(circle.Axis().Direction().Coord())
        return abs(abs(float(direction @ cylinder.axis)) - 1) < PARALLEL


# ---------------------------------------------------------------------------


def _points(edge, count: int | None = None):
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Line

    np = _np()
    curve = BRepAdaptor_Curve(edge)
    t0, t1 = curve.FirstParameter(), curve.LastParameter()
    if count is None:
        count = 2 if curve.GetType() == GeomAbs_Line else 9
    return np.array([curve.Value(t).Coord() for t in np.linspace(t0, t1, count)])


def _radial(point, origin, axis):
    np = _np()
    radial = point - origin
    radial = radial - (radial @ axis) * axis
    return radial / np.linalg.norm(radial)


def _rotation(axis, angle):
    np = _np()
    a = axis / np.linalg.norm(axis)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(angle) * k + (1 - math.cos(angle)) * (k @ k)


def _f(v) -> list[float]:
    return [float(v[0]), float(v[1])]


def _line(a, da, b, db) -> dict:
    return {"kind": "line", "p": [_f(a), _f(b)], "d": [_f(da), _f(db)]}


def _ends(item) -> tuple:
    if item["kind"] == "arc":
        return item["ends"][0], item["ends"][1]
    if item["kind"] == "circle":
        return None, None
    return item["p"][0], item["p"][-1]


def _loops(items: list[dict]) -> list[list[dict]]:
    """The cut's entities joined end to end into closed loops (at K = 0; how
    they join doesn't change with K). A circle is a loop on its own."""
    np = _np()
    loops = [[item] for item in items if item["kind"] == "circle"]
    rest = [item for item in items if item["kind"] != "circle"]
    while rest:
        loop = [rest.pop()]
        start, end = (np.array(p) for p in _ends(loop[0]))
        while np.linalg.norm(start - end) > JOIN:
            for n, item in enumerate(rest):
                a, b = (np.array(p) for p in _ends(item))
                if np.linalg.norm(a - end) <= JOIN:
                    end = b
                elif np.linalg.norm(b - end) <= JOIN:
                    end = a
                else:
                    continue
                loop.append(rest.pop(n))
                break
            else:
                raise NotSheetMetal  # the cut doesn't close
        loops.append(loop)
    return loops


def _loop_area(loop) -> float:
    from . import flat

    return flat.polygon_area(flat.loop_points(loop, 0.5))


def _square_up(data: dict) -> None:
    """Turn the whole pattern so its outline's narrowest bounding rectangle
    lies along X (the longer side) and Y. Turning is the same at every K."""
    from . import flat

    np = _np()
    outer = next(loop for loop in data["loops"] if loop["outer"])
    points = np.array(flat.loop_points(outer["items"], 0.5))
    best = None
    for a, b in zip(points, np.roll(points, -1, axis=0), strict=True):
        if np.linalg.norm(b - a) < 1e-6:
            continue
        angle = -math.atan2(b[1] - a[1], b[0] - a[0])
        turn = np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
        turned = points @ turn.T
        span = turned.max(0) - turned.min(0)
        if span[1] > span[0]:
            angle -= math.pi / 2
            span = span[::-1]
        area = float(span[0] * span[1])
        if best is None or area < best[0] - 1e-9:
            best = (area, angle)
    flat.turn(data, best[1] if best else 0.0)
