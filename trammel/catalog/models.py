"""Product catalog (FR-PRD-1). BOMs arrive with the stock module (FR-STK-6, Phase 2)."""

from __future__ import annotations

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.urls import reverse

from trammel.core import units
from trammel.core.models import BaseModel
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
    cost = RateField(
        default=Decimal("0"),
        validators=[MinValueValidator(0)],
        help_text="What one unit costs you, for margins.",
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

    @classmethod
    def search_filter(cls, term: str) -> Q:
        return Q(name__icontains=term) | Q(sku__icontains=term) | Q(description__icontains=term)
