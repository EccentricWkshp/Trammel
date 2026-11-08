"""Tax rates, jurisdictions and address boundaries (FR-TAX-1..9).

Two kinds of tax (FR-TAX-2):

* **collected** — sales tax charged to the customer and shown on invoices.
* **gross receipts** — owed by the business on its sales (e.g. WA B&O). Never
  on an invoice; computed only in the tax report, by classification.

Rates carry effective dates and are never edited once used: a rate change is a
new row (FR-TAX-1). Invoices store the jurisdiction code and rate they used, so
a later import never changes past invoices (FR-TAX-7).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from trammel.core.models import SingletonModel, uuid7


class Source(models.TextChoices):
    MANUAL = "manual", "Manual"
    CSV = "csv", "CSV import"
    SST = "sst", "SST rate and boundary files"


class ImportBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    source = models.CharField(max_length=8, choices=Source.choices)
    kind = models.CharField(max_length=16, blank=True)  # "rates", "boundaries", "combined"
    filename = models.CharField(max_length=255)
    imported_at = models.DateTimeField(default=timezone.now)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    rows = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    #: Large files (a state's boundary file is hundreds of MB) import in the
    #: background; the screen shows progress from these.
    status = models.CharField(
        max_length=10,
        default="done",
        choices=[
            ("queued", "Queued"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
    )
    error = models.TextField(blank=True)
    source_url = models.URLField(max_length=500, blank=True)

    class Meta:
        ordering = ["-imported_at"]

    def __str__(self) -> str:
        return f"{self.get_source_display()} {self.filename} ({self.imported_at:%Y-%m-%d})"


class Jurisdiction(models.Model):
    class Level(models.TextChoices):
        STATE = "state", "State"
        COUNTY = "county", "County"
        CITY = "city", "City / place"
        DISTRICT = "district", "Special district"
        COMBINED = "combined", "Combined (location code)"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    code = models.CharField(
        max_length=40, unique=True, help_text="e.g. a local tax code such as “2725”."
    )
    name = models.CharField(max_length=120)
    level = models.CharField(max_length=10, choices=Level.choices, default=Level.COMBINED)
    region = models.CharField("state", max_length=2, blank=True)
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.MANUAL)

    class Meta:
        ordering = ["region", "code"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class TaxRate(models.Model):
    class Kind(models.TextChoices):
        COLLECTED = "collected", "Collected from customer (sales tax)"
        GROSS_RECEIPTS = "gross_receipts", "Owed on gross receipts"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    jurisdiction = models.ForeignKey(Jurisdiction, on_delete=models.PROTECT, related_name="rates")
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.COLLECTED)
    name = models.CharField(max_length=120, blank=True, help_text="e.g. “Retail sales tax”.")
    classification = models.CharField(
        max_length=60,
        blank=True,
        help_text="Gross receipts taxes: the classification it applies to, e.g. “Retailing”.",
    )
    rate = models.DecimalField("rate (%)", max_digits=8, decimal_places=4)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.MANUAL)
    batch = models.ForeignKey(ImportBatch, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["jurisdiction__code", "kind", "-effective_from"]
        indexes = [models.Index(fields=["jurisdiction", "kind", "effective_from"])]

    def __str__(self) -> str:
        return f"{self.jurisdiction.code} {self.rate}% from {self.effective_from}"

    def clean(self):
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValidationError("The end date is before the start date.")
        if self.rate is not None and not (Decimal(0) <= self.rate < Decimal(100)):
            raise ValidationError({"rate": "Enter a percentage between 0 and 100."})


class Boundary(models.Model):
    """Postal codes (as 9-digit ZIP+4 ranges) → the jurisdictions that tax them."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    zip_low = models.CharField(max_length=9)
    zip_high = models.CharField(max_length=9)
    jurisdiction_codes = models.JSONField(default=list)
    #: The state's own location code for the area, when the source gives one
    #: (SST composite SER code; a WA location code). Used to group reports.
    location_code = models.CharField(max_length=20, blank=True)
    effective_from = models.DateField(default=dt.date(2000, 1, 1))
    effective_to = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=8, choices=Source.choices)
    batch = models.ForeignKey(ImportBatch, null=True, blank=True, on_delete=models.CASCADE)

    class Meta:
        indexes = [models.Index(fields=["zip_low", "zip_high"])]

    def __str__(self) -> str:
        return f"{self.zip_low}–{self.zip_high}"


class TaxCategory(models.Model):
    """What kind of thing a line is, for taxability (FR-TAX-3) and B&O (FR-TAX-2)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=80, unique=True)
    taxable = models.BooleanField(default=True, help_text="Subject to collected sales tax.")
    gross_receipts_classification = models.CharField(
        max_length=60,
        blank=True,
        help_text="Gross receipts tax classification, e.g. “Retailing”.",
    )
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "tax categories"

    def __str__(self) -> str:
        return self.name

    @classmethod
    def default(cls) -> TaxCategory | None:
        return cls.objects.filter(is_default=True).first()


class TaxSettings(SingletonModel):
    class Sourcing(models.TextChoices):
        DESTINATION = "destination", "Destination-based (most states)"
        ORIGIN = "origin", "Origin-based"

    sourcing = models.CharField(
        max_length=12,
        choices=Sourcing.choices,
        default=Sourcing.DESTINATION,
        help_text="Pickups always use the shop's own location.",
    )

    class ShippingTax(models.TextChoices):
        PRORATED = "prorated", "Taxed in proportion to the taxable items (most states)"
        ALWAYS = "always", "Always taxed"
        NEVER = "never", "Never taxed"

    shipping_tax = models.CharField(
        "shipping and delivery charges",
        max_length=10,
        choices=ShippingTax.choices,
        default=ShippingTax.PRORATED,
        help_text="In proportion: when every item is taxable the whole charge is taxed; "
        "when none is, none of it is.",
    )
    manual_enabled = models.BooleanField("manual rates", default=True)
    csv_enabled = models.BooleanField("CSV imports", default=True)
    sst_enabled = models.BooleanField("SST rate and boundary files", default=True)

    class Meta:
        verbose_name = "tax settings"

    def __str__(self) -> str:
        return "Tax settings"
