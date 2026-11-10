"""
Units of measure (SPEC §4.4, FR-STK-4).

Measurements are stored as entered — `(value, unit)` — plus a canonical value
for comparison. Each unit belongs to one dimension and has an exact factor to
that dimension's canonical unit. Conversions within a dimension are exact
decimal arithmetic; conversions across dimensions raise.

Count units such as "sheet" or "spool" convert to other dimensions, and to each
other, only through item-specific data (a sheet's dimensions, a spool's weight,
how many are in a box). That is the job of the item, not of this table.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction


class IncompatibleUnits(ValueError):
    pass


@dataclass(frozen=True)
class Unit:
    code: str
    label: str
    dimension: str
    #: Canonical units per one of this unit, exact (a minute is 1/60 hour).
    factor: Fraction


# Canonical units: length mm, area mm², volume ml, mass g, count each, time hour.
_UNITS = [
    Unit("mm", "mm", "length", Fraction(Decimal("1"))),
    Unit("cm", "cm", "length", Fraction(Decimal("10"))),
    Unit("m", "m", "length", Fraction(Decimal("1000"))),
    Unit("in", "in", "length", Fraction(Decimal("25.4"))),
    Unit("ft", "ft", "length", Fraction(Decimal("304.8"))),
    Unit("yd", "yd", "length", Fraction(Decimal("914.4"))),
    Unit("linear_ft", "linear ft", "length", Fraction(Decimal("304.8"))),
    Unit("sq_mm", "mm²", "area", Fraction(Decimal("1"))),
    Unit("sq_cm", "cm²", "area", Fraction(Decimal("100"))),
    Unit("sq_m", "m²", "area", Fraction(Decimal("1000000"))),
    Unit("sq_in", "in²", "area", Fraction(Decimal("645.16"))),
    Unit("sq_ft", "ft²", "area", Fraction(Decimal("92903.04"))),
    Unit("sq_yd", "yd²", "area", Fraction(Decimal("836127.36"))),
    Unit("ml", "ml", "volume", Fraction(Decimal("1"))),
    Unit("l", "liter", "volume", Fraction(Decimal("1000"))),
    Unit("cu_in", "in³", "volume", Fraction(Decimal("16.387064"))),
    Unit("cu_ft", "ft³", "volume", Fraction(Decimal("28316.846592"))),
    Unit("cu_yd", "yd³", "volume", Fraction(Decimal("764554.857984"))),
    # US liquid measures, exact (a gallon is 231 in³).
    Unit("fl_oz", "fl oz", "volume", Fraction(Decimal("29.5735295625"))),
    Unit("cup", "cup", "volume", Fraction(Decimal("236.5882365"))),
    Unit("pt", "pint", "volume", Fraction(Decimal("473.176473"))),
    Unit("qt", "quart", "volume", Fraction(Decimal("946.352946"))),
    Unit("gal", "gallon", "volume", Fraction(Decimal("3785.411784"))),
    Unit("board_ft", "board ft", "volume", Fraction(Decimal("2359.737216"))),  # 144 in³
    Unit("g", "g", "mass", Fraction(Decimal("1"))),
    Unit("kg", "kg", "mass", Fraction(Decimal("1000"))),
    Unit("oz", "oz", "mass", Fraction(Decimal("28.349523125"))),
    Unit("lb", "lb", "mass", Fraction(Decimal("453.59237"))),
    Unit("each", "each", "count", Fraction(Decimal("1"))),
    Unit("sheet", "sheet", "count", Fraction(Decimal("1"))),
    Unit("board", "board", "count", Fraction(Decimal("1"))),
    Unit("bar", "bar", "count", Fraction(Decimal("1"))),
    Unit("spool", "spool", "count", Fraction(Decimal("1"))),
    Unit("roll", "roll", "count", Fraction(Decimal("1"))),
    Unit("box", "box", "count", Fraction(Decimal("1"))),
    Unit("hr", "hour", "time", Fraction(Decimal("1"))),
    Unit("min", "minute", "time", Fraction(1, 60)),
]

UNITS: dict[str, Unit] = {u.code: u for u in _UNITS}

UNIT_CHOICES = [
    (dimension.capitalize(), [(u.code, u.label) for u in _UNITS if u.dimension == dimension])
    for dimension in dict.fromkeys(u.dimension for u in _UNITS)
]


def get(code: str) -> Unit:
    try:
        return UNITS[code]
    except KeyError:
        raise IncompatibleUnits(f"Unknown unit {code!r}.") from None


def compatible(a: str, b: str) -> bool:
    """Whether a quantity in `a` converts to `b` with no item-specific data.

    Two different count units never do: a box is not one each, and a spool is
    not one sheet. How many each are in a box belongs to the item.
    """
    src, dst = get(a), get(b)
    if src.dimension == "count":
        return src.code == dst.code
    return src.dimension == dst.dimension


def _to_decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


#: Exact fraction to Decimal, for callers doing their own exact arithmetic.
to_decimal = _to_decimal


def convert(value: Decimal | int | str, from_unit: str, to_unit: str) -> Decimal:
    src, dst = get(from_unit), get(to_unit)
    if not compatible(from_unit, to_unit):
        raise IncompatibleUnits(f"Cannot convert {src.label} to {dst.label}.")
    return _to_decimal(Fraction(Decimal(value)) * src.factor / dst.factor)


def canonical(value: Decimal | int | str, unit: str) -> Decimal:
    return _to_decimal(Fraction(Decimal(value)) * get(unit).factor)
