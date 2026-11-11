"""Stock: locations, lots and the stock ledger (FR-STK-1..3, -8).

What is on hand is the sum of an append-only ledger (`StockMove`). Each lot
keeps a running quantity and value so screens need not add up the ledger;
`services.check_ledger` proves the two agree. Quantities are in the material's
stock unit (`Material.unit`); values are money, and a lot's value reaches
exactly zero when its last unit is used.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone

from trammel.core.models import AppendOnlyModel, BaseModel, SingletonModel, uuid7
from trammel.core.money import MoneyField, RateField

ZERO = Decimal("0")
QTY = {"max_digits": 14, "decimal_places": 4}


class Location(BaseModel):
    """Where stock is kept: a shop, a rack in it, a shelf on the rack."""

    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
        help_text="The location this one is inside.",
    )
    #: "Shop / Rack A / Shelf 2", kept up to date by services.save_location.
    path = models.CharField(max_length=500, editable=False, db_index=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["path"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "parent",
                condition=models.Q(deleted_at=None),
                nulls_distinct=False,
                name="unique_location_name_in_parent",
            )
        ]

    def __str__(self) -> str:
        return self.path or self.name

    def get_absolute_url(self) -> str:
        return reverse("stock:location", args=[self.pk])


class StockItem(models.Model):
    """A material's stock settings: whether it is tracked, and when to restock it."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    # Part of the material: it can go with it while nothing has been stocked.
    material = models.OneToOneField("shop.Material", on_delete=models.CASCADE, related_name="stock")
    tracked = models.BooleanField(
        default=True,
        help_text="Draw it from stock when it is used on a work order, at the cost of the "
        "lots used.",
    )
    min_quantity = models.DecimalField(
        "restock at", null=True, blank=True, help_text="Restock when this much is left.", **QTY
    )
    reorder_quantity = models.DecimalField(
        "reorder", null=True, blank=True, help_text="How much to buy when restocking.", **QTY
    )
    default_location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Where new stock goes unless you say otherwise.",
    )
    #: When staff were told it reached its restock level; cleared once it is above it.
    low_notified_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["material__name"]

    def __str__(self) -> str:
        return str(self.material)

    def get_absolute_url(self) -> str:
        return f"{self.material.get_absolute_url()}?tab=stock"


class Reason(models.TextChoices):
    OPENING = "opening", "Opening balance"
    RECEIVE = "receive", "Received"
    UNRECEIVE = "unreceive", "Receipt undone"
    CONSUME = "consume", "Used"
    RETURN = "return", "Returned to stock"
    ADJUST = "adjust", "Found"
    COUNT = "count", "Counted"
    TRANSFER = "transfer", "Moved"
    REMNANT = "remnant", "Remnant"
    SCRAP = "scrap", "Scrapped"
    VENDOR_RETURN = "vendor_return", "Returned to vendor"
    REVALUE = "revalue", "Cost adjusted"


class StockLot(models.Model):
    """One batch of a material in one place, at one cost. Used first in, first out."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    number = models.CharField(max_length=32, unique=True, editable=False)
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="lots")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="lots")
    #: Orders lots for first in, first out. A lot split off another keeps its date.
    received_on = models.DateField(default=timezone.localdate)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    origin = models.CharField(max_length=16, choices=Reason.choices, editable=False)
    #: The lot this was split from (part of it moved elsewhere).
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="+", editable=False
    )
    vendor = models.ForeignKey(
        "shop.Vendor", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Cost of one stock unit when the lot came in.
    unit_cost = RateField(default=ZERO, editable=False)
    qty_on_hand = models.DecimalField(default=ZERO, editable=False, **QTY)
    value_on_hand = MoneyField(default=ZERO, editable=False)
    note = models.CharField(max_length=300, blank=True)

    # A remnant (FR-STK-5): an offcut with its own size. Its quantity in the stock
    # unit comes from that size. Remnants are used only when picked, never by
    # oldest-first, and are searched by size in either orientation.
    length = models.DecimalField(null=True, blank=True, editable=False, **QTY)
    width = models.DecimalField(null=True, blank=True, editable=False, **QTY)
    dim_unit = models.CharField(max_length=12, blank=True, editable=False)
    #: The longer and shorter side in mm, for searching either way round.
    long_mm = models.DecimalField(
        null=True, blank=True, editable=False, db_index=True, max_digits=14, decimal_places=3
    )
    short_mm = models.DecimalField(
        null=True, blank=True, editable=False, max_digits=14, decimal_places=3
    )

    class Meta:
        ordering = ["received_on", "created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(qty_on_hand__gte=0), name="lot_quantity_not_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(value_on_hand__gte=0), name="lot_value_not_negative"
            ),
            # An empty lot is worth nothing: the last draw takes what value is left.
            models.CheckConstraint(
                condition=models.Q(qty_on_hand__gt=0) | models.Q(value_on_hand=0),
                name="empty_lot_has_no_value",
            ),
        ]
        indexes = [models.Index(fields=["item", "received_on"])]

    def __str__(self) -> str:
        return self.number

    def get_absolute_url(self) -> str:
        return reverse("stock:lot", args=[self.pk])

    @property
    def is_remnant(self) -> bool:
        return self.length is not None

    @property
    def size(self) -> str:
        """“24 × 18 in”, or “30 in” for a piece measured by length only."""
        if self.length is None:
            return ""
        from trammel.core import units
        from trammel.core.money import trim_decimal

        label = units.get(self.dim_unit).label if self.dim_unit else ""
        if self.width is None:
            return f"{trim_decimal(self.length)} {label}"
        return f"{trim_decimal(self.length)} × {trim_decimal(self.width)} {label}"


class StockMove(AppendOnlyModel):
    """One line of the stock ledger. Never changed or deleted (database triggers
    refuse both); a mistake is put right with another move."""

    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="moves")
    lot = models.ForeignKey(StockLot, on_delete=models.PROTECT, related_name="moves")
    #: Where the lot was when this happened.
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=16, choices=Reason.choices)
    #: Signed, in the material's stock unit.
    quantity = models.DecimalField(**QTY)
    #: Signed money in or out of stock.
    value = MoneyField()
    #: What the person entered, when it was in another unit.
    entered_quantity = models.DecimalField(null=True, blank=True, **QTY)
    entered_unit = models.CharField(max_length=12, blank=True)
    note = models.CharField(max_length=300, blank=True)
    #: What caused it, e.g. ("workorders.materialusage", <usage id>).
    source_type = models.CharField(max_length=60, blank=True)
    source_id = models.UUIDField(null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["item", "created_at"]),
            models.Index(fields=["source_type", "source_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_reason_display()} {self.quantity} of {self.item}"


class BOMLine(models.Model):
    """One component of a product made in the shop (FR-STK-6): how much of a
    material goes into one unit of the product. Single level."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    product = models.ForeignKey(
        "catalog.Product", on_delete=models.CASCADE, related_name="bom_lines"
    )
    material = models.ForeignKey("shop.Material", on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(**QTY)
    unit = models.CharField(max_length=12)
    note = models.CharField(max_length=200, blank=True)
    #: The routing step that uses it, so a work order plans it on that step's
    #: operation. Blank: on the product's own operation.
    step = models.ForeignKey(
        "catalog.RoutingStep",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bom_lines",
    )
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="bom_quantity_positive")
        ]

    def __str__(self) -> str:
        return f"{self.quantity} {self.unit} {self.material}"


class StockSettings(SingletonModel):
    class Shortfall(models.TextChoices):
        REFUSE = "refuse", "Refuse, and ask for a count first"
        FOUND = "found", "Record the difference as found stock, at the current cost"

    class PlannedUse(models.TextChoices):
        ASK = "ask", "Ask on the work order once the operation is done"
        AUTO = "auto", "Record them when the operation is marked done"
        OFF = "off", "Do nothing; materials are recorded by hand"

    planned_use = models.CharField(
        "planned materials, such as a product's bill of materials",
        max_length=4,
        choices=PlannedUse.choices,
        default=PlannedUse.ASK,
    )
    shortfall = models.CharField(
        "when a work order uses more than is on hand",
        max_length=8,
        choices=Shortfall.choices,
        default=Shortfall.REFUSE,
    )

    #: Single labels (a label printer's roll), in inches. Sheets are Avery 5160.
    label_width = models.DecimalField(
        "single label width (in)",
        max_digits=5,
        decimal_places=2,
        default=Decimal("2.25"),
        validators=[MinValueValidator(Decimal("1")), MaxValueValidator(Decimal("8"))],
    )
    label_height = models.DecimalField(
        "single label height (in)",
        max_digits=5,
        decimal_places=2,
        default=Decimal("1.25"),
        validators=[MinValueValidator(Decimal("0.5")), MaxValueValidator(Decimal("6"))],
        help_text="The size of one label on your label printer's roll.",
    )

    def __str__(self) -> str:
        return "Stock settings"
