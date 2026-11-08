"""
Money: decimals, never floats, and one rounding rule (SPEC §4.4, FR-TAX-4).

* `MoneyField` holds amounts that appear on documents — totals, line amounts,
  payments — at two decimal places.
* `RateField` holds unit costs and rates that need more precision than a cent
  (filament at $0.024 per gram) at four decimal places. Anything computed from
  a rate is rounded with `round_money()` before it becomes a document amount.
* `PercentField` holds a percentage as entered (12.5 means 12.5 %).

The instance has a single currency (OQ-6), chosen on the setup page.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.db import models

#: The one rounding rule. Half-up to the currency's minor unit (FR-TAX-4).
ROUNDING = ROUND_HALF_UP


def trim_decimal(value: Decimal, min_places: int = 0) -> str:
    """A decimal without padding zeros, but at least `min_places` decimals.

    10.0000 → "10.00" for money (min_places=2), 0.1250 → "0.125", 30.000 → "30".
    """
    if not value.is_finite():
        return str(value)
    whole, _, frac = f"{value.normalize():f}".partition(".")
    frac = frac.ljust(min_places, "0")
    return f"{whole}.{frac}" if frac else whole


def plain_decimal(value: Decimal) -> Decimal:
    """The same number without padding zeros or an exponent: 120.0000 → 120, never 1.2E+2."""
    return Decimal(trim_decimal(value))


def _prepare_decimal(self, value):
    if isinstance(value, Decimal):
        return trim_decimal(value, getattr(self, "min_places", 0))
    return value


# Every decimal input in Trammel shows its value without the database's padding
# zeros ("1.0000" → "1"). Money inputs below keep two places.
forms.DecimalField.prepare_value = _prepare_decimal


class MoneyInput(forms.DecimalField):
    """A form field for an amount or a rate: at least two decimal places shown."""

    min_places = 2


class MoneyField(models.DecimalField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", 14)
        kwargs.setdefault("decimal_places", 2)
        super().__init__(*args, **kwargs)

    def formfield(self, **kwargs):
        return super().formfield(**{"form_class": MoneyInput, **kwargs})


class RateField(models.DecimalField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", 14)
        kwargs.setdefault("decimal_places", 4)
        super().__init__(*args, **kwargs)

    def formfield(self, **kwargs):
        return super().formfield(**{"form_class": MoneyInput, **kwargs})


class PercentField(models.DecimalField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_digits", 7)
        kwargs.setdefault("decimal_places", 3)
        super().__init__(*args, **kwargs)


def currency_exponent(currency: str) -> int:
    """Minor-unit digits for a currency: 2 for USD, 0 for JPY, 3 for KWD."""
    from babel.numbers import get_currency_precision

    return get_currency_precision(currency)


def round_money(amount: Decimal | int | str, currency: str | None = None) -> Decimal:
    """Round an amount to the currency's minor unit, half-up."""
    if currency is None:
        currency = _instance_currency()
    exponent = currency_exponent(currency)
    quantum = Decimal(1).scaleb(-exponent)
    return Decimal(amount).quantize(quantum, rounding=ROUNDING)


def format_money(amount: Decimal | None, currency: str | None = None, locale: str = "en_US") -> str:
    from babel.numbers import format_currency

    if amount is None:
        return ""
    if currency is None:
        currency = _instance_currency()
    return format_currency(amount, currency, locale=locale)


def _instance_currency() -> str:
    from .models import BusinessProfile

    return BusinessProfile.load().currency
