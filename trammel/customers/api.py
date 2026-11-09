from __future__ import annotations

import datetime as dt
from uuid import UUID

from ninja import ModelSchema, Router, Schema, Status
from ninja.pagination import LimitOffsetPagination, paginate

from trammel.api.types import choices
from trammel.core import access, audit

from . import services
from .models import Address, Contact, Customer

router = Router(tags=["customers"])


class ContactOut(ModelSchema):
    class Meta:
        model = Contact
        fields = ["id", "name", "role", "email", "phone", "is_primary", "notes"]


class AddressOut(ModelSchema):
    class Meta:
        model = Address
        fields = [
            "id",
            "label",
            "line1",
            "line2",
            "city",
            "region",
            "postal_code",
            "country",
            "is_default_billing",
            "is_default_shipping",
        ]


class CustomerOut(ModelSchema):
    class Meta:
        model = Customer
        fields = [
            "id",
            "kind",
            "name",
            "company",
            "email",
            "phone",
            "notes",
            "payment_terms_days",
            "tax_exempt",
            "tax_exempt_number",
            "tax_exempt_expires",
            "is_placeholder",
            "created_at",
            "updated_at",
        ]


class CustomerDetailOut(CustomerOut):
    contacts: list[ContactOut]
    addresses: list[AddressOut]

    @staticmethod
    def resolve_contacts(obj):
        return list(obj.contacts.all())

    @staticmethod
    def resolve_addresses(obj):
        return list(obj.addresses.all())


class CustomerIn(Schema):
    kind: choices(Customer.Kind) = Customer.Kind.INDIVIDUAL
    name: str
    company: str = ""
    email: str = ""
    phone: str = ""
    notes: str = ""
    payment_terms_days: int | None = None
    tax_exempt: bool = False
    tax_exempt_number: str = ""
    tax_exempt_expires: dt.date | None = None


class CustomerPatch(Schema):
    kind: choices(Customer.Kind) | None = None
    name: str | None = None
    company: str | None = None
    email: str | None = None
    phone: str | None = None
    notes: str | None = None
    payment_terms_days: int | None = None
    tax_exempt: bool | None = None
    tax_exempt_number: str | None = None
    tax_exempt_expires: dt.date | None = None


@router.get("/", response=list[CustomerOut])
@paginate(LimitOffsetPagination)
def list_customers(request, q: str = ""):
    qs = Customer.objects.all()
    if q.strip():
        qs = qs.filter(Customer.search_filter(q.strip()))
    return access.scoped(request.actor, "customers.view", qs)


@router.get("/{customer_id}", response=CustomerDetailOut)
def get_customer(request, customer_id: UUID):
    return access.get_or_404(
        request.actor, "customers.view", Customer.objects.all(), pk=customer_id
    )


@router.post("/", response={201: CustomerDetailOut})
def create_customer(request, payload: CustomerIn):
    access.require(request.actor, "customers.create")
    customer = services.save_customer(Customer(**payload.dict()), before=None, actor=request.actor)
    return Status(201, customer)


@router.patch("/{customer_id}", response=CustomerDetailOut)
def update_customer(request, customer_id: UUID, payload: CustomerPatch):
    customer = access.get_or_404(
        request.actor, "customers.edit", Customer.objects.all(), pk=customer_id
    )
    before = audit.snapshot(customer)
    for field, value in payload.dict(exclude_unset=True).items():
        setattr(customer, field, value)
    return services.save_customer(customer, before=before)


@router.delete("/{customer_id}", response={204: None})
def delete_customer(request, customer_id: UUID):
    customer = access.get_or_404(
        request.actor, "customers.delete", Customer.objects.all(), pk=customer_id
    )
    services.delete_customer(customer)
    return Status(204, None)
