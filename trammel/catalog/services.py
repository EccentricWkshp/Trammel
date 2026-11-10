"""Catalog services."""

from __future__ import annotations

from decimal import Decimal

from trammel.core.money import round_money
from trammel.core.services import save_audited

from .models import Product


def create_product(**fields) -> Product:
    """A new catalog product, validated and audited (from a quote line, say)."""
    fields["price"] = round_money(fields.get("price") or Decimal("0"))
    return save_audited(Product(**fields))


def save_adhoc(
    *,
    description: str,
    price: Decimal,
    cost: Decimal = Decimal("0"),
    unit: str = "each",
    tax_category=None,
) -> tuple[Product, bool]:
    """Keep a one-off quote or sale line as a catalog product (FR-PRD-2).

    The product is named after the line's first line of text. An active product
    with that name already is reused, not duplicated. Returns (product, created).
    """
    name = (description.strip().splitlines() or [""])[0].strip()[:200]
    existing = Product.objects.filter(name__iexact=name, is_active=True).first()
    if existing is not None:
        return existing, False
    product = save_audited(
        Product(
            name=name,
            description=description.strip() if description.strip() != name else "",
            price=round_money(price),
            cost=cost or Decimal("0"),
            unit=unit,
            tax_category=tax_category,
            kind=Product.Kind.MADE,
        )
    )
    return product, True
