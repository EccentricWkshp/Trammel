"""Catalog services."""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction

from trammel.core import audit
from trammel.core.money import round_money
from trammel.core.services import save_audited

from .models import Product


def in_use(product: Product) -> bool:
    """Whether a quote, invoice or other kept record points at the product.

    Found through the relations other modules declare, so the catalog needs no
    knowledge of them. Its own parts (a bill of materials) don't count.
    """
    for rel in Product._meta.get_fields(include_hidden=True):
        # Reverse foreign keys, including those named "+" (quote and invoice lines).
        if not (rel.auto_created and not rel.concrete and (rel.one_to_many or rel.one_to_one)):
            continue
        if rel.on_delete is models.CASCADE:
            continue
        model = rel.related_model
        if model._base_manager.filter(**{rel.field.name: product}).exists():
            return True
    return False


@transaction.atomic
def set_archived(product: Product, archived: bool) -> Product:
    """Archive a product (it stays on old quotes and invoices but can't be
    picked for new ones) or bring it back."""
    before = audit.snapshot(product)
    product.is_active = not archived
    return save_audited(product, before=before)


@transaction.atomic
def delete_product(product: Product, *, reason: str = "") -> None:
    """Move a product nothing has used to the trash."""
    if in_use(product):
        raise ValidationError(
            "This product is on quotes or invoices, so it can't be deleted. "
            "Archive it instead: it stays on those but can't be picked for new ones."
        )
    product.delete()
    audit.record_delete(product, reason=reason)


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
