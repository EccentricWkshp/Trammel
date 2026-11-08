"""Tax determination and calculation (FR-TAX-3, -4, -6..9).

No determination makes an outbound call (FR-TAX-6): every rate comes from the
manual table or an imported file already in the database.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Q

from trammel.core.money import round_money

from .models import Boundary, Jurisdiction, Source, TaxRate, TaxSettings


@dataclass
class Component:
    code: str
    name: str
    rate: Decimal  # percent


@dataclass
class Determination:
    """The rates that apply to a sale. Stored on the invoice as a snapshot."""

    resolved: bool
    postal_code: str = ""
    location_code: str = ""
    components: list[Component] = field(default_factory=list)
    method: str = ""  # "address", "manual", "exempt", "unresolved"
    note: str = ""

    @property
    def total_rate(self) -> Decimal:
        return sum((c.rate for c in self.components), Decimal("0"))

    @property
    def short_label(self) -> str:
        """What a customer sees: one combined line, no jurisdiction codes."""
        return f"Sales tax ({rate_text(self.total_rate)})"

    @property
    def label(self) -> str:
        if not self.components:
            return "No tax" if self.resolved else "Not resolved"
        code = self.location_code or ", ".join(c.code for c in self.components)
        return f"{code} ({self.total_rate.normalize():f}%)"


def rate_text(rate: Decimal) -> str:
    """10.5000 -> '10.5%'."""
    text = f"{Decimal(str(rate)).normalize():f}"
    return f"{text}%"


def taxable_shipping(shipping: Decimal, taxable: Decimal, base: Decimal) -> Decimal:
    """The part of a shipping or delivery charge that is taxed (TaxSettings.shipping_tax).

    `taxable` and `base` are the taxable and total amounts of the items shipped.
    """
    if shipping <= 0:
        return Decimal("0")
    mode = TaxSettings.load().shipping_tax
    if mode == TaxSettings.ShippingTax.ALWAYS:
        return shipping
    if mode == TaxSettings.ShippingTax.NEVER or base <= 0 or taxable <= 0:
        return Decimal("0")
    return min(shipping, round_money(shipping * taxable / base))


def normalize_postal(value: str) -> str | None:
    """'98101', '98101-1234' or '981011234' → 9 digits (ZIP5 padded with 0000)."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 5:
        return digits + "0000"
    if len(digits) == 9:
        return digits
    return None


def _effective(qs, on: dt.date):
    return qs.filter(effective_from__lte=on).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gte=on)
    )


def collected_rates(codes: list[str], on: dt.date) -> list[Component]:
    out = []
    for code in codes:
        jurisdiction = Jurisdiction.objects.filter(code=code).first()
        if jurisdiction is None:
            continue
        rate = (
            _effective(jurisdiction.rates.filter(kind=TaxRate.Kind.COLLECTED), on)
            .order_by("-effective_from")
            .first()
        )
        if rate is not None:
            out.append(Component(jurisdiction.code, jurisdiction.name, rate.rate))
    return out


def lookup_postal(postal: str, on: dt.date) -> Determination:
    zip9 = normalize_postal(postal)
    if zip9 is None:
        return Determination(False, postal, method="unresolved", note="Not a US ZIP or ZIP+4 code.")
    settings = TaxSettings.load()
    sources = [
        s
        for s, on_ in (
            (Source.CSV, settings.csv_enabled),
            (Source.SST, settings.sst_enabled),
            (Source.MANUAL, settings.manual_enabled),
        )
        if on_
    ]
    # Walk the index backward from this ZIP: the ranges that contain it start
    # just below it, so a state's hundreds of thousands of rows are never scanned.
    candidates = _effective(
        Boundary.objects.filter(zip_low__lte=zip9, zip_high__gte=zip9, source__in=sources), on
    ).order_by("-zip_low")[:50]
    # The narrowest range wins: a ZIP+4 match beats a whole-ZIP match.
    best = min(candidates, key=lambda b: int(b.zip_high) - int(b.zip_low), default=None)
    if best is None:
        return Determination(
            False, postal, method="unresolved", note=f"No tax boundary covers {postal}."
        )
    components = collected_rates(best.jurisdiction_codes, on)
    if not components:
        return Determination(
            False,
            postal,
            best.location_code,
            method="unresolved",
            note="The boundary's jurisdictions have no rate on this date.",
        )
    return Determination(True, postal, best.location_code, components, method="address")


def manual(jurisdiction: Jurisdiction, on: dt.date, note: str = "") -> Determination:
    components = collected_rates([jurisdiction.code], on)
    return Determination(
        bool(components),
        "",
        jurisdiction.code,
        components,
        method="manual",
        note=note or "Rate chosen by hand.",
    )


def determine(
    *, fulfillment: str, destination_postal: str, shop_postal: str, on: dt.date
) -> Determination:
    """Apply the sourcing rule (FR-TAX-8), then look the address up."""
    settings = TaxSettings.load()
    origin = fulfillment == "pickup" or settings.sourcing == TaxSettings.Sourcing.ORIGIN
    return lookup_postal(shop_postal if origin else destination_postal, on)


@dataclass
class TaxLine:
    code: str
    name: str
    rate: Decimal
    base: Decimal
    amount: Decimal


def calculate(
    taxable_base: Decimal, determination: Determination, currency: str | None = None
) -> list[TaxLine]:
    """Tax per rate, each rounded half-up to the cent (FR-TAX-4)."""
    return [
        TaxLine(
            c.code,
            c.name,
            c.rate,
            taxable_base,
            round_money(taxable_base * c.rate / Decimal(100), currency),
        )
        for c in determination.components
    ]
