"""Deleting a buyer's data at a platform's request (FR-IMP-6, FR-IMP-7).

eBay sends a "marketplace account deletion" notice whenever any eBay account
closes, buyer of yours or not. Trammel doesn't host the public address eBay
sends to: a relay forwards each notice to the API, or staff enter one by hand.

On arrival the buyer's details in eBay's stored orders (what the importer
read) are wiped at once. Customer records are never changed automatically (decided
11/11/2026): one may also be a customer in their own right, so each request
waits on the review page for staff to anonymize its customers or dismiss it.
A notice for someone who never bought from you is closed as not a match.

Anonymizing keeps the money: sales, payments, tax figures and the tax location
stay, as eBay allows for legal (tax) records. The name, email, phone, contacts
and street addresses go.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from trammel.core import audit

from .models import DeletionRequest, ImportedOrder
from .services import forget_buyer

ANONYMIZED_NAME = "Deleted buyer"


def receive(
    *,
    username: str = "",
    user_id: str = "",
    eias_token: str = "",
    notification_id: str = "",
    event_date=None,
    via: str = DeletionRequest.Via.HAND,
    source: str = "ebay",
) -> tuple[DeletionRequest, bool]:
    """Record a deletion request and wipe the buyer's details from the stored
    orders. Returns (request, is_new): a notice delivered twice is handled once."""
    username, user_id = (username or "").strip(), (user_id or "").strip()
    if not (username or user_id):
        raise ValidationError("Enter the eBay username of the closed account.")
    notification_id = (notification_id or "").strip()[:100]
    if notification_id:
        known = DeletionRequest.objects.filter(
            source=source, notification_id=notification_id
        ).first()
        if known is not None:
            return known, False
    try:
        with transaction.atomic():
            orders = forget_buyer(source, username) if username else 0
            row = DeletionRequest.objects.create(
                source=source,
                username=username[:100],
                user_id=user_id[:100],
                eias_token=(eias_token or "")[:200],
                notification_id=notification_id,
                event_date=event_date,
                via=via,
                orders=orders,
                status=DeletionRequest.Status.OPEN if orders else DeletionRequest.Status.NO_MATCH,
                resolved_at=None if orders else timezone.now(),
            )
    except IntegrityError:
        # The same notice, delivered twice at once.
        return DeletionRequest.objects.get(source=source, notification_id=notification_id), False
    if orders:
        audit.record(
            "import.deletion_request",
            entity_type="imports.deletionrequest",
            entity_id=str(row.pk),
            entity_label=str(row),
            summary=f"{row.get_source_display()} asked to delete a buyer's data: the details in "
            f"{orders} stored order(s) were wiped; the customer waits for review",
        )
    return row, True


def candidates(row: DeletionRequest) -> list[dict]:
    """The customers this request's orders went to, with the rest of their
    business, so staff can see whether they are also customers in their own right."""
    from trammel.billing.models import Invoice
    from trammel.jobs.models import Job

    if not row.username:
        return []
    customer_ids = (
        ImportedOrder.objects.filter(source=row.source, buyer_id=row.username)
        .exclude(customer=None)
        .values_list("customer", flat=True)
        .distinct()
    )
    from trammel.customers.models import Customer

    found = []
    for customer in Customer.objects.filter(pk__in=list(customer_ids)).order_by("name"):
        imported = ImportedOrder.objects.filter(customer=customer)
        found.append(
            {
                "customer": customer,
                "done": str(customer.pk) in row.anonymized,
                "platform_orders": imported.filter(source=row.source).count(),
                "other_store_orders": imported.exclude(source=row.source).count(),
                "other_invoices": Invoice.objects.filter(customer=customer)
                .filter(imported_order__isnull=True)
                .count(),
                "jobs": Job.objects.filter(customer=customer)
                .exclude(pk__in=imported.exclude(job=None).values("job"))
                .count(),
            }
        )
    return found


@transaction.atomic
def anonymize(row: DeletionRequest, customer, *, user=None) -> None:
    """Wipe a customer's personal details, keeping every sale and tax figure."""
    from trammel.billing.models import Invoice
    from trammel.customers.models import Address, Contact, Customer

    if row.status != DeletionRequest.Status.OPEN:
        raise ValidationError("This request has already been dealt with.")
    if customer.pk not in [c["customer"].pk for c in candidates(row)]:
        raise ValidationError("That customer isn't part of this request.")
    label = f"{ANONYMIZED_NAME} {str(customer.pk)[-6:]}"
    Customer.objects.filter(pk=customer.pk).update(
        name=label, company="", email="", phone="", notes="", updated_at=timezone.now()
    )
    Contact.all_objects.filter(customer=customer).update(
        name="Deleted contact", role="", email="", phone="", notes=""
    )
    # The street goes; the city, state and ZIP stay with the tax records.
    Address.all_objects.filter(customer=customer).update(label="", line1="(deleted)", line2="")
    Invoice.all_objects.filter(customer=customer).update(ship_to_text="")
    for order in ImportedOrder.objects.filter(customer=customer):
        payload = dict(order.payload)
        payload["customer"] = {k: "" for k in (payload.get("customer") or {})}
        order.payload = payload
        order.save(update_fields=["payload", "updated_at"])
    row.anonymized = [*row.anonymized, str(customer.pk)]
    if all(c["done"] or c["customer"].pk == customer.pk for c in candidates(row)):
        row.status = DeletionRequest.Status.DONE
        row.resolved_at, row.resolved_by = timezone.now(), user
    row.save()
    audit.record(
        "customer.anonymize",
        entity_type="customers.customer",
        entity_id=str(customer.pk),
        entity_label=label,
        summary=f"Anonymized a customer at {row.get_source_display()}'s request: name, email, "
        "phone, contacts and street addresses wiped; sales and tax records kept",
    )


def dismiss(row: DeletionRequest, *, note: str, user=None) -> None:
    """Close a request without anonymizing (say, the customer is also a direct
    customer whose details you keep for your own business)."""
    if row.status != DeletionRequest.Status.OPEN:
        raise ValidationError("This request has already been dealt with.")
    note = (note or "").strip()
    if not note:
        raise ValidationError("Say why the request is dismissed.")
    row.status = DeletionRequest.Status.DISMISSED
    row.note = note[:2000]
    row.resolved_at, row.resolved_by = timezone.now(), user
    row.save()
    audit.record(
        "import.deletion_dismissed",
        entity_type="imports.deletionrequest",
        entity_id=str(row.pk),
        entity_label=str(row),
        summary=f"Dismissed {row}: {row.note}",
    )


def open_count() -> int:
    return DeletionRequest.objects.filter(status=DeletionRequest.Status.OPEN).count()
