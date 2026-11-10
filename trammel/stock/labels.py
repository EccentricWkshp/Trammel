"""Labels for lots, locations and materials (FR-STK-7).

Each label carries a QR code of the record's short link (`/s/<id>/`), so a
phone, tablet or hand scanner opens it on the shop floor. Two layouts:

* "sheet": Avery 5160 and compatibles — US letter, 3 across and 10 down,
  2⅝ × 1 in. Printing can start at any position, so a part-used sheet is not
  wasted.
* "single": one label per page, at the size set in stock settings, for a
  label printer.

Positions are absolute, in inches, so the browser's print and WeasyPrint lay
the sheet out the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

from trammel.core import formats, qr, scan

# Avery 5160: letter, 3 columns × 10 rows.
SHEET_COLUMNS, SHEET_ROWS = 3, 10
PER_SHEET = SHEET_COLUMNS * SHEET_ROWS
LABEL_W, LABEL_H = Decimal("2.625"), Decimal("1")
TOP, LEFT = Decimal("0.5"), Decimal("0.1875")
PITCH_X, PITCH_Y = Decimal("2.75"), Decimal("1")

MAX_COPIES = 50


@dataclass
class Label:
    title: str
    code: str
    lines: list[str] = field(default_factory=list)
    qr: str = ""
    #: Position on a sheet, in inches, set by `sheets`.
    top: Decimal = Decimal("0")
    left: Decimal = Decimal("0")


def _label(pk, title: str, code: str, lines: list[str]) -> Label:
    return Label(title=title, code=code, lines=[x for x in lines if x], qr=qr.svg(scan.link(pk)))


def for_lots(lots) -> list[Label]:
    return [
        _label(
            lot.pk,
            str(lot.item.material),
            lot.number,
            [
                f"Remnant {lot.size}" if lot.is_remnant else "",
                str(lot.location),
                f"Received {formats.show(lot.received_on)}",
            ],
        )
        for lot in lots
    ]


def for_locations(locations) -> list[Label]:
    return [
        _label(loc.pk, loc.name, "", [f"In {loc.parent.path}" if loc.parent_id else ""])
        for loc in locations
    ]


def for_materials(materials) -> list[Label]:
    return [_label(m.pk, str(m), m.sku, [m.get_category_display()]) for m in materials]


def sheets(labels: list[Label], *, start: int = 1, copies: int = 1) -> list[list[Label]]:
    """Labels placed on Avery 5160 sheets, skipping the first `start - 1` positions."""
    start = max(1, min(int(start), PER_SHEET))
    copies = max(1, min(int(copies), MAX_COPIES))
    placed, pages = [label for label in labels for _ in range(copies)], []
    position = start - 1
    page: list[Label] = []
    for label in placed:
        if position == PER_SHEET:
            pages.append(page)
            page, position = [], 0
        row, col = divmod(position, SHEET_COLUMNS)
        page.append(replace(label, top=TOP + PITCH_Y * row, left=LEFT + PITCH_X * col))
        position += 1
    if page:
        pages.append(page)
    return pages


def singles(labels: list[Label], *, copies: int = 1) -> list[Label]:
    copies = max(1, min(int(copies), MAX_COPIES))
    return [label for label in labels for _ in range(copies)]
