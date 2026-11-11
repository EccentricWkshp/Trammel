"""Product catalog (FR-PRD-1): products, their routings (the steps it takes to
make one) and their cost. Bills of materials arrive with the stock module
(FR-STK-6)."""

from __future__ import annotations

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.urls import reverse

from trammel.core import units
from trammel.core.models import BaseModel, uuid7
from trammel.core.money import MoneyField, RateField


class Product(BaseModel):
    class Kind(models.TextChoices):
        MADE = "made", "Made in the shop"
        RESOLD = "resold", "Resold"
        SERVICE = "service", "Service"

    sku = models.CharField("SKU", max_length=64, blank=True)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.MADE)
    description = models.TextField(blank=True)
    price = MoneyField(default=Decimal("0"), validators=[MinValueValidator(0)])
    #: What one unit costs: the override if there is one, otherwise calculated
    #: from the bill of materials, routing and other cost (catalog.costing).
    #: Quotes, invoices and reports read this.
    cost = RateField(default=Decimal("0"), validators=[MinValueValidator(0)], editable=False)
    cost_override = RateField(
        "cost override",
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text="What one unit costs you, if you'd rather set it than have it calculated "
        "from the bill of materials and routing. Leave blank to calculate it.",
    )
    other_cost = RateField(
        "other cost per unit",
        default=Decimal("0"),
        validators=[MinValueValidator(0)],
        help_text="Anything else one unit costs: packaging, finishing supplies, or what you "
        "pay for an item you resell.",
    )
    batch_size = models.PositiveIntegerField(
        "usual batch size",
        default=1,
        validators=[MinValueValidator(1)],
        help_text="How many you usually make at once. Setup time is spread over these.",
    )
    unit = models.CharField(max_length=12, choices=units.UNIT_CHOICES, default="each")
    tax_category = models.ForeignKey(
        "tax.TaxCategory",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Leave blank for the default category.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["sku"],
                condition=~Q(sku="") & Q(deleted_at__isnull=True),
                name="unique_live_product_sku",
            )
        ]

    def __str__(self) -> str:
        return f"{self.sku} {self.name}".strip()

    def get_absolute_url(self) -> str:
        return reverse("catalog:edit", args=[self.pk])

    @property
    def cost_is_calculated(self) -> bool:
        return self.cost_override is None

    @classmethod
    def search_filter(cls, term: str) -> Q:
        return Q(name__icontains=term) | Q(sku__icontains=term) | Q(description__icontains=term)


class RoutingStep(models.Model):
    """One step it takes to make the product: what is done, on which machine, and
    how long it takes (Phase 2b). A work order made from a quote with the product
    gets each step as an operation."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="routing_steps")
    position = models.PositiveIntegerField(default=0)
    name = models.CharField(max_length=200)
    instructions = models.TextField(blank=True)
    machine = models.ForeignKey(
        "shop.Machine", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    #: Once per batch, however many are made.
    setup_minutes = models.DecimalField(
        max_digits=9, decimal_places=2, default=Decimal("0"), validators=[MinValueValidator(0)]
    )
    #: Per unit: the machine running, attended or not.
    machine_minutes = models.DecimalField(
        max_digits=9, decimal_places=2, default=Decimal("0"), validators=[MinValueValidator(0)]
    )
    #: Per unit: hands-on time.
    labor_minutes = models.DecimalField(
        max_digits=9, decimal_places=2, default=Decimal("0"), validators=[MinValueValidator(0)]
    )
    #: Once per batch: time the work sits before the next step can start. Nobody
    #: works and no machine runs, so it costs nothing, but it takes time.
    wait_hours = models.DecimalField(
        "wait after (hours)",
        max_digits=8,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(0)],
        help_text="How long the work has to sit after this step before the next can start: "
        "glue clamping, finish curing, paint drying. 12 for overnight. It adds nothing to the "
        "cost, but production estimates count it, around the clock.",
    )

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(setup_minutes__gte=0)
                & Q(machine_minutes__gte=0)
                & Q(labor_minutes__gte=0),
                name="routing_minutes_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(wait_hours__gte=0), name="routing_wait_not_negative"
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def planned_labor(self, quantity) -> Decimal:
        """Minutes of hands-on time to make `quantity`: setup once, plus each one."""
        return self.setup_minutes + self.labor_minutes * Decimal(quantity)

    def planned_machine(self, quantity) -> Decimal:
        return self.machine_minutes * Decimal(quantity)

    def planned_work(self, quantity) -> Decimal:
        """Minutes of working time to make `quantity`, hands-on and machine one after
        the other."""
        return self.planned_labor(quantity) + self.planned_machine(quantity)
