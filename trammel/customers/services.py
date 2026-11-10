"""Customer writes, shared by the web views and the API. Each is audit-logged."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction

from trammel.core import audit

from .models import Address, Contact, Customer


@transaction.atomic
def save_customer(customer: Customer, *, before: dict | None, actor=None) -> Customer:
    creating = customer._state.adding
    if creating and actor is not None and getattr(actor, "user", None) is not None:
        customer.created_by = actor.user
    customer.full_clean()
    customer.save()
    if creating:
        audit.record_create(customer)
    else:
        audit.record_update(customer, before or {})
    return customer


@transaction.atomic
def delete_customer(customer: Customer, *, reason: str = "") -> None:
    if customer.is_placeholder:
        raise ValidationError("The walk-in placeholder customer cannot be deleted.")
    if customer.is_internal:
        raise ValidationError("The shop's internal customer cannot be deleted.")
    customer.delete()
    audit.record_delete(customer, reason=reason)


@transaction.atomic
def restore_customer(customer: Customer) -> None:
    customer.restore()
    audit.record_restore(customer)


@transaction.atomic
def save_contact(contact: Contact, *, before: dict | None) -> Contact:
    creating = contact._state.adding
    contact.full_clean()
    contact.save()
    if contact.is_primary:
        contact.customer.contacts.exclude(pk=contact.pk).update(is_primary=False)
    (audit.record_create(contact) if creating else audit.record_update(contact, before or {}))
    return contact


@transaction.atomic
def save_address(address: Address, *, before: dict | None) -> Address:
    creating = address._state.adding
    address.full_clean()
    address.save()
    others = address.customer.addresses.exclude(pk=address.pk)
    if address.is_default_billing:
        others.update(is_default_billing=False)
    if address.is_default_shipping:
        others.update(is_default_shipping=False)
    (audit.record_create(address) if creating else audit.record_update(address, before or {}))
    return address


@transaction.atomic
def delete_child(obj: Contact | Address) -> None:
    obj.delete()
    audit.record_delete(obj)
