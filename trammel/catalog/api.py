from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Router, Schema, Status
from ninja.pagination import LimitOffsetPagination, paginate

from trammel.api.types import choices
from trammel.core import access, archive, audit
from trammel.core.services import save_audited

from .models import Product

router = Router(tags=["products"])


class ProductOut(ModelSchema):
    class Meta:
        model = Product
        fields = [
            "id",
            "sku",
            "name",
            "kind",
            "description",
            "price",
            "cost",
            "unit",
            "tax_category",
            "is_active",
        ]


class ProductIn(Schema):
    name: str
    sku: str = ""
    kind: choices(Product.Kind) = Product.Kind.MADE
    description: str = ""
    price: Decimal = Decimal("0")
    cost: Decimal = Decimal("0")
    unit: str = "each"
    tax_category: UUID | None = None
    is_active: bool = True


def _apply(product: Product, payload: ProductIn) -> Product:
    data = payload.dict()
    product.tax_category_id = data.pop("tax_category")
    for field, value in data.items():
        setattr(product, field, value)
    return product


@router.get("/", response=list[ProductOut])
@paginate(LimitOffsetPagination)
def list_products(request, q: str = ""):
    qs = Product.objects.all()
    if q.strip():
        qs = qs.filter(Product.search_filter(q.strip()))
    return access.scoped(request.actor, "catalog.view", qs)


@router.post("/", response={201: ProductOut})
def create_product(request, payload: ProductIn):
    access.require(request.actor, "catalog.edit")
    return Status(201, save_audited(_apply(Product(), payload)))


@router.put("/{product_id}", response=ProductOut)
def update_product(request, product_id: UUID, payload: ProductIn):
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=product_id)
    before = audit.snapshot(product)
    return save_audited(_apply(product, payload), before=before)


class ArchiveIn(Schema):
    archived: bool = True


@router.post("/{product_id}/archive", response=ProductOut)
def archive_product(request, product_id: UUID, payload: ArchiveIn):
    """Archive a product (kept where it is used, not offered for new quotes) or bring it back."""
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=product_id)
    return archive.set_archived(product, payload.archived)


@router.delete("/{product_id}", response={204: None})
def delete_product(request, product_id: UUID):
    """Delete a product nothing has used yet; one on quotes or invoices is refused."""
    product = access.get_or_404(request.actor, "catalog.edit", Product.objects.all(), pk=product_id)
    archive.delete_unused(product)
    return Status(204, None)
