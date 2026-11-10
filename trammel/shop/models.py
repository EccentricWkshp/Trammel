"""Machines and the Phase 1 materials price list (SPEC §5, FR-ADM-4, §7.6 note).

Until the stock module exists, quote and job material costs come from this
price list. Phase 2 replaces it with actual lot costs; the estimators ask a
material for its cost per unit, so they will not need to change.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse

from trammel.core import units
from trammel.core.models import BaseModel, uuid7
from trammel.core.money import MoneyField, PercentField, RateField, trim_decimal

NONNEG = MinValueValidator(0)


class Machine(BaseModel):
    """A shop resource with an hourly rate, used by estimators and time tracking."""

    class Kind(models.TextChoices):
        LASER = "laser", "Laser cutter / engraver"
        PRINTER_3D = "printer_3d", "3D printer"
        CNC = "cnc", "CNC router / mill"
        LATHE = "lathe", "Lathe"
        SAW = "saw", "Saw"
        WELDER = "welder", "Welder"
        PLASMA = "plasma", "Plasma cutter"
        OTHER = "other", "Other"

    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.OTHER)
    hourly_rate = RateField(
        default=Decimal("0"),
        validators=[MinValueValidator(0)],
        help_text="Charged per hour of machine time, on top of labor.",
    )
    is_active = models.BooleanField(
        default=True, help_text="Inactive machines are kept for history but not offered."
    )
    notes = models.TextField(blank=True)

    # Rate calculator inputs (trammel.shop.rates). All optional.
    purchase_cost = MoneyField("purchase price", null=True, blank=True, validators=[NONNEG])
    salvage_value = MoneyField(
        "resale value",
        null=True,
        blank=True,
        validators=[NONNEG],
        help_text="What it should sell for at the end of its life.",
    )
    lifespan_hours = models.PositiveIntegerField(
        "expected life (hours)",
        null=True,
        blank=True,
        help_text="Running hours before it is replaced.",
    )
    annual_hours = models.PositiveIntegerField(
        "hours run per year",
        null=True,
        blank=True,
        help_text="Spreads the yearly fixed costs.",
    )
    maintenance_cost = MoneyField(
        "maintenance cost",
        null=True,
        blank=True,
        validators=[NONNEG],
        help_text="Parts and labor for one service.",
    )
    maintenance_interval_hours = models.PositiveIntegerField(
        "service every (hours)", null=True, blank=True
    )
    #: [{"name", "cost", "life_hours"}] — tubes, nozzles, bits, blades…
    wear_parts = models.JSONField(default=list, blank=True)
    power_kw = models.DecimalField(
        "average power draw (kW)",
        max_digits=7,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[NONNEG],
        help_text="Include chillers, exhaust and dust collection that run with it.",
    )
    consumables_per_hour = RateField(
        "consumables per hour",
        null=True,
        blank=True,
        validators=[NONNEG],
        help_text="Gas, wire, compressed air and the like.",
    )
    annual_fixed_costs = MoneyField(
        "yearly fixed costs",
        null=True,
        blank=True,
        validators=[NONNEG],
        help_text="This machine's insurance, software subscriptions, service contract.",
    )
    failure_pct = PercentField(
        "failure / scrap allowance (%)",
        null=True,
        blank=True,
        help_text="Time lost to failed runs and remakes.",
    )
    markup_pct = PercentField("markup (%)", null=True, blank=True)

    CALCULATOR_FIELDS = (
        "purchase_cost",
        "salvage_value",
        "lifespan_hours",
        "annual_hours",
        "maintenance_cost",
        "maintenance_interval_hours",
        "wear_parts",
        "power_kw",
        "consumables_per_hour",
        "annual_fixed_costs",
        "failure_pct",
        "markup_pct",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("shop:machine_edit", args=[self.pk])

    def rate_breakdown(self):
        from trammel.core.models import ShopRates

        from .rates import calculate

        inputs = {f: getattr(self, f) for f in self.CALCULATOR_FIELDS}
        return calculate(inputs, ShopRates.load().electricity_rate)

    @property
    def calculated_cost(self) -> Decimal | None:
        """Cost per hour from the rate calculator, or None if it has not been filled in."""
        breakdown = self.rate_breakdown()
        return breakdown.cost if breakdown.has_inputs else None

    @property
    def cost_rate(self) -> Decimal:
        """What an hour of this machine costs the shop; its charge rate if not worked out."""
        cost = self.calculated_cost
        return self.hourly_rate if cost is None else cost


class Vendor(BaseModel):
    """Who the shop buys from (FR-CUS-5). Purchasing builds on this in Phase 2."""

    name = models.CharField(max_length=150)
    contact_name = models.CharField("contact", max_length=150, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    website = models.URLField(max_length=300, blank=True)
    account_number = models.CharField(
        "your account number", max_length=60, blank=True, help_text="Your account with them."
    )
    payment_terms = models.CharField(
        max_length=60, blank=True, help_text="e.g. “Net 30”, “Credit card”."
    )
    lead_time_days = models.PositiveSmallIntegerField(
        "typical lead time (days)", null=True, blank=True
    )
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("shop:vendor_edit", args=[self.pk])


class Material(BaseModel):
    """A price-list entry: what a material costs per unit."""

    class Category(models.TextChoices):
        SHEET = "sheet", "Sheet goods"
        LUMBER = "lumber", "Lumber"
        METAL = "metal", "Metal stock"
        FILAMENT = "filament", "3D printing filament / resin"
        HARDWARE = "hardware", "Hardware / fasteners"
        FINISH = "finish", "Finishes / adhesives"
        OTHER = "other", "Other"

    name = models.CharField(max_length=150)
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.OTHER)
    sku = models.CharField("SKU / vendor part number", max_length=64, blank=True)
    vendor = models.ForeignKey(
        Vendor,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="materials",
        help_text="Who you usually buy it from.",
    )
    url = models.URLField(
        "product page", max_length=500, blank=True, help_text="Where to look it up or reorder."
    )
    unit = models.CharField(max_length=12, choices=units.UNIT_CHOICES, default="each")
    unit_cost = RateField("cost per unit", default=Decimal("0"), validators=[MinValueValidator(0)])
    markup = PercentField(
        "markup (%)",
        null=True,
        blank=True,
        help_text="Leave blank to use the default markup on materials.",
    )
    # Sheet goods: the size of one sheet, so a cost per sheet can be priced by
    # area (the laser estimator works in area).
    sheet_width = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    sheet_length = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    sheet_dimension_unit = models.CharField(
        max_length=12,
        blank=True,
        choices=[(u.code, u.label) for u in units.UNITS.values() if u.dimension == "length"],
    )
    thickness = models.CharField(max_length=32, blank=True, help_text="e.g. “3 mm”, “3/4 in”")
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self) -> str:
        return f"{self.name} {self.thickness}".strip()

    def get_absolute_url(self) -> str:
        return reverse("shop:material", args=[self.pk])

    def clean(self):
        dims = [self.sheet_width, self.sheet_length]
        if any(dims) and not (all(dims) and self.sheet_dimension_unit):
            raise ValidationError("Give both sheet dimensions and their unit, or neither.")

    @property
    def sheet_area_sq_mm(self) -> Decimal | None:
        if not (self.sheet_width and self.sheet_length and self.sheet_dimension_unit):
            return None
        w = units.canonical(self.sheet_width, self.sheet_dimension_unit)
        h = units.canonical(self.sheet_length, self.sheet_dimension_unit)
        return w * h

    # -- Conversions (FR-STK-4) ----------------------------------------------
    #
    # `unit` is the stock unit: what the cost is per, and what stock is counted
    # in. Other units convert to it within a dimension, through the sheet size
    # (a sheet's area), or through the material's own conversions ("1 box =
    # 100 each", "1000 g = 1 spool", "1 linear ft = 0.5 board ft").

    def _conversions(self) -> list[MaterialUnit]:
        cache = getattr(self, "_prefetched_objects_cache", {})
        if "conversions" in cache:
            return list(cache["conversions"])
        if self.pk is None:
            return []
        if not hasattr(self, "_conversion_cache"):
            self._conversion_cache = list(self.conversions.all())
        return self._conversion_cache

    def _per_stock(self, unit: str) -> Fraction:
        """Stock units in one `unit`, exactly. Raises IncompatibleUnits."""
        u = units.get(unit)
        if units.compatible(unit, self.unit):
            return u.factor / units.get(self.unit).factor
        if u.dimension == "area" and units.get(self.unit).dimension == "count":
            area = self.sheet_area_sq_mm
            if area:
                return u.factor / Fraction(area)
        for c in self._conversions():
            if units.compatible(unit, c.unit):
                return (
                    u.factor
                    / units.get(c.unit).factor
                    * Fraction(c.stock_quantity)
                    / Fraction(c.quantity)
                )
        raise units.IncompatibleUnits(
            f"{self} is counted in {units.get(self.unit).label}; "
            f"there is no conversion from {u.label}."
        )

    def per_stock_unit(self, unit: str) -> Decimal:
        """How many stock units one `unit` is. Raises IncompatibleUnits."""
        return units.to_decimal(self._per_stock(unit))

    def convert(self, value, from_unit: str, to_unit: str) -> Decimal:
        """`value` in `from_unit` expressed in `to_unit`, through the stock unit if needed."""
        if units.compatible(from_unit, to_unit):
            return units.convert(value, from_unit, to_unit)
        ratio = self._per_stock(from_unit) / self._per_stock(to_unit)
        return units.to_decimal(Fraction(Decimal(value)) * ratio)

    def to_stock(self, value, unit: str) -> Decimal:
        return self.convert(value, unit, self.unit)

    def convertible_units(self) -> list[str]:
        """Unit codes a quantity of this material can be entered in."""
        out = []
        for code in units.UNITS:
            if units.get(code).dimension == "time":
                continue
            try:
                self.per_stock_unit(code)
            except units.IncompatibleUnits:
                continue
            out.append(code)
        return out

    def unit_choices(self) -> list[tuple[str, str]]:
        return [(c, units.get(c).label) for c in self.convertible_units()]

    def cost_per(self, unit: str) -> Decimal:
        """Cost of one `unit` of this material, unrounded.

        Converts within a dimension, from a per-sheet cost to an area cost when
        the sheet size is known, and through the material's own conversions.
        Raises IncompatibleUnits otherwise.
        """
        try:
            return self.unit_cost * self.per_stock_unit(unit)
        except units.IncompatibleUnits:
            raise units.IncompatibleUnits(
                f"{self} is priced per {units.get(self.unit).label}; "
                f"cannot price it per {units.get(unit).label}."
            ) from None

    def effective_markup(self, rates) -> Decimal:
        return self.markup if self.markup is not None else rates.default_material_markup


POSITIVE = MinValueValidator(Decimal("0.0001"))


class MaterialUnit(models.Model):
    """`quantity unit` of this material is `stock_quantity` of its stock unit.

    "100 each = 1 box", "1000 g = 1 spool", "1 linear ft = 0.5 board ft". One
    row covers a whole dimension: a spool given in grams also converts from kg.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    material = models.ForeignKey(Material, on_delete=models.CASCADE, related_name="conversions")
    quantity = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal("1"), validators=[POSITIVE]
    )
    unit = models.CharField(max_length=12, choices=units.UNIT_CHOICES)
    stock_quantity = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal("1"), validators=[POSITIVE]
    )

    class Meta:
        ordering = ["unit"]
        constraints = [
            models.UniqueConstraint(fields=["material", "unit"], name="one_conversion_per_unit"),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0, stock_quantity__gt=0),
                name="conversion_positive",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{trim_decimal(self.quantity)} {units.get(self.unit).label} = "
            f"{trim_decimal(self.stock_quantity)} {units.get(self.material.unit).label}"
        )

    def clean(self):
        """Refuse a conversion the material can already make (within a dimension,
        through its sheet size, or through another row): two routes could disagree."""
        if not self.unit or self.material_id is None:
            return
        material = self.material
        material._conversion_cache = list(material.conversions.exclude(pk=self.pk))
        try:
            material.per_stock_unit(self.unit)
        except units.IncompatibleUnits:
            return
        finally:
            del material._conversion_cache
        raise ValidationError(
            {
                "unit": f"{material} already converts from {units.get(self.unit).label} "
                f"to {units.get(material.unit).label}."
            }
        )


class MaterialBarcode(models.Model):
    """A manufacturer or shop barcode, and how much of the material one scan is."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    material = models.ForeignKey(Material, on_delete=models.CASCADE, related_name="barcodes")
    code = models.CharField(max_length=64, unique=True)
    quantity = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal("1"), validators=[POSITIVE]
    )
    unit = models.CharField(max_length=12, choices=units.UNIT_CHOICES, blank=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return self.code

    def save(self, *args, **kwargs):
        self.code = self.code.strip()
        if not self.unit:
            self.unit = self.material.unit
        super().save(*args, **kwargs)

    def clean(self):
        self.code = self.code.strip()
        if self.unit:
            try:
                self.material.per_stock_unit(self.unit)
            except units.IncompatibleUnits as exc:
                raise ValidationError({"unit": str(exc)}) from None
