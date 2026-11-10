"""Purchase orders, their lines, and what arrived (FR-PUR-1..5).

A purchase order is drafted, placed with the vendor (it gets its number then),
received in one or more deliveries, and closed. Tax, shipping and a discount
are spread over the lines by value, to the cent, giving each line its landed
cost; a delivery brings in its share of that, and the last delivery of a line
brings in exactly what is left. Receipts are append-only: undoing one writes a
reversing receipt.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse

from trammel.core import units
from trammel.core.lifecycle import Lifecycle, LifecycleModel, Transition
from trammel.core.models import AppendOnlyModel, BaseModel, uuid7
from trammel.core.money import MoneyField, RateField

ZERO = Decimal("0")
QTY = {"max_digits": 14, "decimal_places": 4}


class Status(models.TextChoices):
    DRAFT = "draft", "Draft"
    ORDERED = "ordered", "Ordered"
    CLOSED = "closed", "Closed"
    CANCELLED = "cancelled", "Canceled"


def _has_lines(po):
    if not po.lines.exists():
        yield "Add a line before placing the order."


def _tax_has_a_taxable_line(po):
    if po.tax and not po.lines.filter(tax_exempt=False).exists():
        yield "Tax is entered, but every line is tax exempt. Remove the tax or a line's exemption."


def _exemption_on_file(po):
    if po.tax_exempt:
        from django.utils import timezone

        from trammel.core.models import BusinessProfile

        if problem := BusinessProfile.load().resale_certificate_problem(timezone.localdate()):
            yield problem


def _nothing_received(po):
    if po.receipts.exists():
        yield "Something has been received on this order; close it instead."


PO_LIFECYCLE = Lifecycle(
    field="status",
    initial=Status.DRAFT,
    transitions=[
        # Placing an order goes through services, which allocates the number.
        Transition(
            Status.DRAFT,
            Status.ORDERED,
            permission="purchasing.order",
            label="Place order",
            guards=(_has_lines, _tax_has_a_taxable_line, _exemption_on_file),
        ),
        Transition(Status.ORDERED, Status.CLOSED, permission="purchasing.order", label="Close"),
        Transition(
            (Status.DRAFT, Status.ORDERED),
            Status.CANCELLED,
            permission="purchasing.order",
            requires_reason=True,
            label="Cancel",
            guards=(_nothing_received,),
        ),
        Transition(
            Status.CLOSED,
            Status.ORDERED,
            kind="reopen",
            permission="purchasing.order",
            requires_reason=True,
            label="Reopen",
        ),
    ],
)


class PurchaseOrder(LifecycleModel, BaseModel):
    vendor = models.ForeignKey(
        "shop.Vendor", on_delete=models.PROTECT, related_name="purchase_orders"
    )
    number = models.CharField(max_length=32, blank=True, editable=False)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT, editable=False
    )
    ordered_on = models.DateField(null=True, blank=True, editable=False)
    expected_on = models.DateField("expected", null=True, blank=True)
    vendor_reference = models.CharField(
        "vendor's order number",
        max_length=60,
        blank=True,
        help_text="Their confirmation or order number.",
    )
    tax_exempt = models.BooleanField(
        "the whole order is tax exempt",
        default=False,
        help_text="Bought for resale or under an exemption certificate. Its tax is zero.",
    )
    exemption_number = models.CharField(
        "exemption certificate number",
        max_length=60,
        blank=True,
        help_text="Printed on the order for the vendor.",
    )
    tax = MoneyField(default=ZERO, validators=[MinValueValidator(0)])
    shipping = MoneyField(default=ZERO, validators=[MinValueValidator(0)])
    discount = MoneyField(default=ZERO, validators=[MinValueValidator(0)])
    notes = models.TextField(blank=True)

    lifecycle = PO_LIFECYCLE
    soft_delete_cascade = ()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["number"], condition=~models.Q(number=""), name="unique_po_number"
            )
        ]

    def __str__(self) -> str:
        return self.number or f"Draft order to {self.vendor}"

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.tax_exempt and self.tax:
            raise ValidationError({"tax": "The order is tax exempt, so its tax is zero."})

    def get_absolute_url(self) -> str:
        return reverse("purchasing:detail", args=[self.pk])

    @property
    def charges(self) -> Decimal:
        """Tax and shipping less the discount: what is spread over the lines."""
        return self.tax + self.shipping - self.discount

    @property
    def is_open(self) -> bool:
        return self.status == Status.ORDERED

    @property
    def editable(self) -> bool:
        """Lines and charges change until the first delivery arrives."""
        return self.status in (Status.DRAFT, Status.ORDERED) and not self.receipts.exists()


class POLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField(default=0)
    material = models.ForeignKey(
        "shop.Material", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    description = models.CharField(max_length=200)
    vendor_sku = models.CharField("vendor SKU", max_length=64, blank=True)
    quantity = models.DecimalField(validators=[MinValueValidator(Decimal("0.0001"))], **QTY)
    unit = models.CharField(max_length=12, choices=units.UNIT_CHOICES, default="each")
    unit_price = RateField("price per unit", validators=[MinValueValidator(0)])
    #: Bought for a job: it goes onto this work order when it arrives.
    work_order = models.ForeignKey(
        "workorders.WorkOrder",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="purchase_lines",
    )
    tax_exempt = models.BooleanField(
        default=False,
        help_text="Takes no share of the order's tax: bought for resale, say.",
    )
    update_cost = models.BooleanField(
        "make this the material's cost",
        default=True,
        help_text="Quotes use a material's latest cost.",
    )

    class Meta:
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.description

    @property
    def goods(self) -> Decimal:
        from trammel.core.money import round_money

        return round_money(self.quantity * self.unit_price)


class PurchaseReceipt(AppendOnlyModel):
    """Part or all of a line arriving, or the undoing of an earlier arrival."""

    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="receipts")
    line = models.ForeignKey(POLine, on_delete=models.PROTECT, related_name="receipts")
    #: In the line's unit; negative when it undoes `reverses`.
    quantity = models.DecimalField(**QTY)
    #: The landed cost that came in with it (negative when undone).
    value = MoneyField()
    received_on = models.DateField()
    lot = models.ForeignKey(
        "stock.StockLot", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: The material's cost before this receipt changed it, put back if it is undone.
    previous_unit_cost = RateField(null=True, blank=True)
    reverses = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.quantity} {self.line.unit} of {self.line}"


class VendorItem(models.Model):
    """What a vendor calls a material, and what it last cost from them."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    vendor = models.ForeignKey("shop.Vendor", on_delete=models.CASCADE, related_name="items")
    material = models.ForeignKey(
        "shop.Material", on_delete=models.CASCADE, related_name="vendor_items"
    )
    vendor_sku = models.CharField(max_length=64, blank=True)
    last_price = RateField(null=True, blank=True)
    last_unit = models.CharField(max_length=12, blank=True)
    last_ordered_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["vendor__name"]
        constraints = [
            models.UniqueConstraint(fields=["vendor", "material"], name="one_vendor_item")
        ]

    def __str__(self) -> str:
        return f"{self.material} from {self.vendor}"


class VendorReturn(AppendOnlyModel):
    """Part of a delivery sent back to the vendor (FR-PUR-5). The stock leaves at its
    value; the refund may be more or less, and the difference is a cost variance."""

    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="returns")
    line = models.ForeignKey(POLine, on_delete=models.PROTECT, related_name="returns")
    lot = models.ForeignKey(
        "stock.StockLot", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: In the line's unit.
    quantity = models.DecimalField(**QTY)
    #: What the returned goods were worth in stock (their landed cost).
    value = MoneyField()
    refund = MoneyField(validators=[MinValueValidator(0)])
    reason = models.CharField(max_length=300)
    returned_on = models.DateField()
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["created_at"]

    @property
    def loss(self) -> Decimal:
        return self.value - self.refund


class LandedAdjustment(AppendOnlyModel):
    """A change to an order's landed cost after it arrived: a late freight bill, a
    corrected invoice. Spread over the lines; stock still on hand is revalued."""

    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="adjustments")
    amount = MoneyField()
    reason = models.CharField(max_length=300)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["created_at"]


class CostVariance(AppendOnlyModel):
    """Cost that could not go into stock: a refund for less than the goods were worth,
    or a landed cost change for goods already used. Positive is a cost to the shop."""

    class Kind(models.TextChoices):
        RETURN = "return", "Vendor return"
        LANDED = "landed", "Landed cost adjustment"

    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="variances")
    line = models.ForeignKey(POLine, on_delete=models.PROTECT, related_name="variances")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    amount = MoneyField()
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["created_at"]
