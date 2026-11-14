"""Flat patterns of sheet metal parts (FR-DOC-4, 0.11.2), laid out for a K-factor.

unfold.py keeps a part's flat pattern at K = 0 with each point's change per
unit of K; this lays it out for the K-factor the shop's material gives, and
makes what's shown and sent from it: the shop's numbers (blank size and area,
cut length, pierces, bends), a picture (SVG) and a DXF for the cutting CAM.
Needs no OpenCascade, only numpy and ezdxf, so pages can do it at once.

Coordinates are millimeters, the pattern seen from its skin (the side of the
largest flat face): a bend marked up folds towards the viewer.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

#: Arcs are followed every this many degrees where a polygon is needed.
ARC_STEP = 3.0


@dataclass
class Bend:
    angle: float  # degrees
    radius: float  # inside, mm
    #: How far the bend runs across the part, along its fold (mm).
    length: float
    up: bool
    line: list  # [[x, y], [x, y]]
    #: The flat sheet the bend takes up, at the neutral line: angle (in
    #: radians) × (inside radius + K × thickness), mm.
    allowance: float = 0.0


@dataclass
class Pattern:
    k_factor: float
    thickness: float
    #: [(outer?, [entity, ...])], entities resolved for this K.
    loops: list
    extents: list
    bends: list
    low: list = field(default_factory=list)
    high: list = field(default_factory=list)
    #: The blank: its outline's area less its holes', mm².
    area: float = 0.0
    #: Every loop of the cut, mm.
    cut_length: float = 0.0

    @property
    def size(self) -> list[float]:
        return [self.high[0] - self.low[0], self.high[1] - self.low[1]]

    @property
    def pierces(self) -> int:
        return len(self.loops)

    @property
    def holes(self) -> int:
        return len(self.loops) - 1


# ---------------------------------------------------------------------------
# Laying out


def _at(entity: dict, k: float) -> dict:
    kind = entity["kind"]
    if kind in ("circle", "arc"):
        out = dict(entity)
        out["c"] = [entity["c"][0] + k * entity["dc"][0], entity["c"][1] + k * entity["dc"][1]]
        return out
    points = [
        [p[0] + k * d[0], p[1] + k * d[1]] for p, d in zip(entity["p"], entity["d"], strict=True)
    ]
    return {"kind": kind, "p": points}


def _arc_points(entity: dict) -> list[list[float]]:
    a0 = entity["a0"]
    sweep = (entity["a1"] - a0) % 360 or 360
    count = max(2, int(math.ceil(sweep / ARC_STEP)) + 1)
    cx, cy, r = entity["c"][0], entity["c"][1], entity["r"]
    return [
        [cx + r * math.cos(math.radians(a0 + sweep * i / (count - 1))),
         cy + r * math.sin(math.radians(a0 + sweep * i / (count - 1)))]
        for i in range(count)
    ]  # fmt: skip


def points_of(entity: dict) -> list[list[float]]:
    """An entity (already laid out) as points, start to end."""
    if entity["kind"] == "circle":
        e = dict(entity, a0=0.0, a1=360.0)
        return _arc_points(e)
    if entity["kind"] == "arc":
        return _arc_points(entity)
    return entity["p"]


def length_of(entity: dict) -> float:
    if entity["kind"] == "circle":
        return 2 * math.pi * entity["r"]
    if entity["kind"] == "arc":
        sweep = (entity["a1"] - entity["a0"]) % 360 or 360
        return math.radians(sweep) * entity["r"]
    p = entity["p"]
    return sum(math.dist(a, b) for a, b in zip(p, p[1:], strict=False))


def loop_points(items: list[dict], k: float) -> list[list[float]]:
    """A loop's entities, laid out for K, as one polygon in order."""
    out: list[list[float]] = []
    for item in items:
        points = points_of(_at(item, k))
        if out:
            if math.dist(points[-1], out[-1]) < math.dist(points[0], out[-1]):
                points = points[::-1]
            points = points[1:]
        out += points
    return out


def polygon_area(points) -> float:
    total = 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1], strict=True):
        total += x0 * y1 - x1 * y0
    return total / 2


def pattern(data: dict, k_factor: float) -> Pattern:
    """The flat pattern laid out for a K-factor (0 to 0.5, sometimes more)."""
    k = float(k_factor)
    loops = [(loop["outer"], [_at(item, k) for item in loop["items"]]) for loop in data["loops"]]
    extents = [_at(item, k) for item in data["extents"]]
    bends = [
        Bend(
            angle=b["angle"],
            radius=b["radius"],
            length=b["length"],
            up=b["up"],
            line=[[p[0] + k * d[0], p[1] + k * d[1]] for p, d in zip(b["p"], b["d"], strict=True)],
            allowance=math.radians(b["angle"]) * (b["radius"] + k * data["thickness"]),
        )
        for b in data["bends"]
    ]
    result = Pattern(k, data["thickness"], loops, extents, bends)
    outline = []
    area = 0.0
    for outer, items in loops:
        polygon = []
        for item in items:
            points = points_of(item)
            if polygon and math.dist(points[-1], polygon[-1]) < math.dist(points[0], polygon[-1]):
                points = points[::-1]
            polygon += points if not polygon else points[1:]
            result.cut_length += length_of(item)
        # A round hole exactly; anything else from its outline's polygon.
        if len(items) == 1 and items[0]["kind"] == "circle":
            loop_area = math.pi * items[0]["r"] ** 2
        else:
            loop_area = abs(polygon_area(polygon))
        if outer:
            outline = polygon
            area += loop_area
        else:
            area -= loop_area
    result.area = area
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    result.low, result.high = [min(xs), min(ys)], [max(xs), max(ys)]
    return result


def turn(data: dict, angle: float) -> None:
    """Turn a stored pattern (at K = 0 and its change per K) about the origin."""
    c, s = math.cos(angle), math.sin(angle)

    def r(v):
        return [c * v[0] - s * v[1], s * v[0] + c * v[1]]

    def entity(e):
        if e["kind"] in ("circle", "arc"):
            e["c"], e["dc"] = r(e["c"]), r(e["dc"])
            if e["kind"] == "arc":
                e["a0"] += math.degrees(angle)
                e["a1"] += math.degrees(angle)
                e["ends"] = [r(p) for p in e["ends"]]
        else:
            e["p"] = [r(p) for p in e["p"]]
            e["d"] = [r(d) for d in e["d"]]

    for loop in data["loops"]:
        for item in loop["items"]:
            entity(item)
    for item in data["extents"]:
        entity(item)
    for bend in data["bends"]:
        bend["p"] = [r(p) for p in bend["p"]]
        bend["d"] = [r(d) for d in bend["d"]]


# ---------------------------------------------------------------------------
# The shop's numbers


def _num(value: float, places: int) -> str:
    text = f"{value:,.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _length(mm: float, metric: bool, places_in: int = 3, places_mm: int = 1) -> str:
    return f"{_num(mm, places_mm)} mm" if metric else f"{_num(mm / 25.4, places_in)} in"


def _both(mm: float, metric: bool, places_in: int = 3, places_mm: int = 1) -> str:
    """In the shop's units, then the other system's."""
    first = _length(mm, metric, places_in, places_mm)
    second = _length(mm, not metric, places_in, places_mm)
    return f"{first} ({second})"


def facts(p: Pattern, *, metric: bool = False) -> dict:
    """What the shop prices a part from, as text in its units."""
    w, h = p.size
    area_text = (
        f"{_num(p.area / 100, 1)} cm² ({_num(p.area / 645.16, 2)} in²)"
        if metric
        else f"{_num(p.area / 645.16, 2)} in² ({_num(p.area / 100, 1)} cm²)"
    )
    if metric:
        size = f"{_num(w, 1)} × {_num(h, 1)} mm ({_num(w / 25.4, 3)} × {_num(h / 25.4, 3)} in)"
    else:
        size = f"{_num(w / 25.4, 3)} × {_num(h / 25.4, 3)} in ({_num(w, 1)} × {_num(h, 1)} mm)"
    holes = p.holes
    return {
        "size": size,
        "area": area_text,
        "cut": _both(p.cut_length, metric, 2),
        "pierces": f"{p.pierces} (the outline"
        + (f" and {holes} hole{'s' if holes != 1 else ''})" if holes else ")"),
        "thickness": _both(p.thickness, metric, 4, 2),
        "bends": [
            {
                "number": n,
                "direction": "Up" if b.up else "Down",
                "angle": f"{_degrees(b.angle)}°",
                "radius": _both(b.radius, metric, 4, 2),
                "line": _length(b.length, metric),
                "allowance": _both(b.allowance, metric, 4, 2),
            }
            for n, b in enumerate(p.bends, start=1)
        ],
    }


#: Units by the names 3MF and STEP use (meshes.UNITS_MM), as words.
UNIT_NAMES = {
    "inch": "inches",
    "foot": "feet",
    "millimeter": "millimeters",
    "centimeter": "centimeters",
    "meter": "meters",
    "micron": "microns",
}


# ---------------------------------------------------------------------------
# A picture


def svg(p: Pattern, *, width_px: int = 960) -> str:
    """The flat pattern as an SVG: the cut in dark lines, bends dashed with
    their angle and direction. Seen from the skin, the same way as the DXF."""
    margin = max(p.size) * 0.04 + 1
    x0, y0 = p.low[0] - margin, p.low[1] - margin
    w, h = p.size[0] + 2 * margin, p.size[1] + 2 * margin
    label = max(min(w, h) * 0.035, max(w, h) * 0.012)

    def y(v):  # SVG's y runs down
        return y0 + h - (v - y0)

    def path(entity) -> str:
        pts = points_of(entity)
        return "M" + " L".join(f"{a:.3f},{y(b):.3f}" for a, b in pts)

    stroke = max(w, h) / width_px * 1.6
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.3f} {y0:.3f} {w:.3f} {h:.3f}" '
        f'width="{width_px}" height="{round(width_px * h / w)}" role="img">',
        f'<rect x="{x0:.3f}" y="{y0:.3f}" width="{w:.3f}" height="{h:.3f}" fill="#fff"/>',
    ]
    cut = " ".join(path(item) for _outer, items in p.loops for item in items)
    parts.append(
        f'<path d="{cut}" fill="none" stroke="#1d2b3a" stroke-width="{stroke:.4f}" '
        'stroke-linejoin="round"/>'
    )
    if p.extents:
        extent = " ".join(path(item) for item in p.extents)
        parts.append(
            f'<path d="{extent}" fill="none" stroke="#9aa5b1" stroke-width="{stroke * 0.6:.4f}"/>'
        )
    for n, bend in enumerate(p.bends, start=1):
        (ax, ay), (bx, by) = bend.line
        parts.append(
            f'<line x1="{ax:.3f}" y1="{y(ay):.3f}" x2="{bx:.3f}" y2="{y(by):.3f}" '
            f'stroke="#b3261e" stroke-width="{stroke:.4f}" '
            f'stroke-dasharray="{stroke * 6:.3f} {stroke * 4:.3f}"/>'
        )
        # Beside the line, not on it: off its middle, square to it.
        length = math.hypot(bx - ax, by - ay) or 1.0
        nx, ny = -(by - ay) / length, (bx - ax) / length
        if nx < 0:
            nx, ny = -nx, -ny  # to the right of an upright line, above a level one
        # Beside an upright line, the label starts just clear of it; across a
        # level one, it's centered just above.
        anchor = "start" if nx > 0.5 else "middle"
        gap = label * (0.6 if anchor == "start" else 1.0)
        mx = (ax + bx) / 2 + nx * gap
        my = (ay + by) / 2 + ny * gap
        text = f"{n}: {'UP' if bend.up else 'DOWN'} {_degrees(bend.angle)}°"
        place = (
            f'x="{mx:.3f}" y="{y(my) + label * 0.35:.3f}" font-size="{label:.3f}" '
            f'text-anchor="{anchor}" font-family="sans-serif"'
        )
        # A white halo under the label, as text of its own (PDF renderers
        # don't all honor paint-order), then the label.
        parts.append(
            f'<text {place} fill="#fff" stroke="#fff" stroke-width="{label * 0.25:.3f}">'
            f"{text}</text>"
        )
        parts.append(f'<text {place} fill="#b3261e">{text}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _degrees(angle: float) -> str:
    text = f"{angle:.1f}"
    return text[:-2] if text.endswith(".0") else text


# ---------------------------------------------------------------------------
# A DXF


#: Layer names as Fusion and other CAD programs export flat patterns, so CAM
#: setups that pick them out keep working.
LAYERS = {
    "outer": ("OUTER_PROFILES", 7),
    "interior": ("INTERIOR_PROFILES", 5),
    "bend": ("BEND", 1),
    "extent": ("BEND_EXTENT", 8),
}

#: DXF's $INSUNITS codes, and millimeters in each.
DXF_UNITS = {"inch": (1, 25.4), "foot": (2, 304.8), "millimeter": (4, 1.0),
             "centimeter": (5, 10.0), "meter": (6, 1000.0), "micron": (13, 0.001)}  # fmt: skip


def dxf(p: Pattern, unit: str = "millimeter") -> bytes:
    """The flat pattern as a DXF (R2010) in the part's own unit: the cut as
    lines, arcs and circles on OUTER_PROFILES and INTERIOR_PROFILES, bends on
    BEND (dashed) and their extents on BEND_EXTENT, from the origin up."""
    import ezdxf

    code, scale = DXF_UNITS.get(unit, DXF_UNITS["millimeter"])
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = code
    doc.header["$MEASUREMENT"] = 0 if code in (1, 2) else 1
    for name, color in LAYERS.values():
        doc.layers.add(name, color=color)
    doc.layers.get("BEND").dxf.linetype = "DASHED" if "DASHED" in doc.linetypes else "CONTINUOUS"
    msp = doc.modelspace()
    ox, oy = p.low

    def at(v):
        return ((v[0] - ox) / scale, (v[1] - oy) / scale)

    def add(entity, layer):
        attribs = {"layer": layer}
        if entity["kind"] == "line":
            msp.add_line(at(entity["p"][0]), at(entity["p"][1]), dxfattribs=attribs)
        elif entity["kind"] == "circle":
            msp.add_circle(at(entity["c"]), entity["r"] / scale, dxfattribs=attribs)
        elif entity["kind"] == "arc":
            msp.add_arc(
                at(entity["c"]), entity["r"] / scale, entity["a0"], entity["a1"], dxfattribs=attribs
            )
        else:
            msp.add_lwpolyline([at(v) for v in entity["p"]], dxfattribs=attribs)

    for outer, items in p.loops:
        layer = LAYERS["outer" if outer else "interior"][0]
        for item in items:
            add(item, layer)
    for item in p.extents:
        add(item, LAYERS["extent"][0])
    for bend in p.bends:
        msp.add_line(at(bend.line[0]), at(bend.line[1]), dxfattribs={"layer": LAYERS["bend"][0]})
    out = io.StringIO()
    doc.write(out)
    return out.getvalue().encode()
