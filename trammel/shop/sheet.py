"""Sheet metal (0.11.2): a material's thickness read from its text, and the
material, and so the K-factor, for a part of a given thickness.

A material's thickness is free text ("3 mm", "1/8 in", "3/16\"", "0.1875",
"1-1/2 in"); with no unit it's in the shop's units. A gauge ("11 ga") means a
different thickness in each metal, so it matches nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

#: When no material of a part's thickness has a K-factor: a common one for
#: mild steel in an air bend, and what Fusion starts with.
DEFAULT_K = Decimal("0.44")

#: A material matches a part this close in thickness: a share of it, or this
#: many mm for thin sheet.
MATCH_SHARE = 0.015
MATCH_MM = 0.02

_UNITS = {
    "mm": 1.0,
    "millimeter": 1.0,
    "millimeters": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "inch": 25.4,
    "inches": 25.4,
    '"': 25.4,
    "″": 25.4,
}
_NUMBER = r"(?:(?P<whole>\d+)[\s-]+)?(?P<num>\d+(?:\.\d*)?|\.\d+)(?:\s*/\s*(?P<den>\d+))?"
_THICKNESS = re.compile(rf"^\s*{_NUMBER}\s*(?P<unit>[a-z\"″]*)\.?\s*$", re.IGNORECASE)


def thickness_mm(text: str, metric: bool = False) -> float | None:
    """A thickness in mm from a material's text, or None when it can't say."""
    match = _THICKNESS.match(text or "")
    if not match:
        return None
    unit = match["unit"].lower()
    if unit and unit not in _UNITS:
        return None  # a gauge, or a unit that isn't a length
    value = float(match["num"])
    if match["den"]:
        if not float(match["den"]):
            return None
        value /= float(match["den"])
    if match["whole"]:
        if not match["den"]:
            return None  # "1 2" isn't a number
        value += float(match["whole"])
    scale = _UNITS[unit] if unit else (1.0 if metric else 25.4)
    return value * scale if value > 0 else None


def matches(material_mm: float | None, part_mm: float) -> bool:
    if material_mm is None:
        return False
    return abs(material_mm - part_mm) <= max(MATCH_MM, part_mm * MATCH_SHARE)


@dataclass
class Choice:
    """The K-factor for a part, and where it came from."""

    k_factor: Decimal
    material: object | None = None
    #: Materials of the part's thickness with a K-factor (any can be chosen).
    options: list = field(default_factory=list)
    #: Materials of the part's thickness without one (worth setting).
    without_k: list = field(default_factory=list)


def choose(thickness: float, chosen=None, metric: bool = False) -> Choice:
    """The K-factor for a part this thickness (mm): from the material chosen for
    it if that's still its thickness, else the only (or first) active material
    its thickness with a K-factor, else DEFAULT_K."""
    from .models import Material

    options, without = [], []
    for material in Material.objects.filter(is_active=True).order_by("name"):
        if not matches(thickness_mm(material.thickness, metric), thickness):
            continue
        (options if material.k_factor is not None else without).append(material)
    pick = None
    if chosen is not None and any(m.pk == chosen.pk for m in options):
        pick = chosen
    elif options:
        pick = options[0]
    return Choice(pick.k_factor if pick else DEFAULT_K, pick, options, without)
