"""Online payments (0.10.x): the provider's settings, the payment links made for
invoices, and the payments that came back through them."""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from trammel.core.models import SingletonModel, uuid7
from trammel.core.money import MoneyField


class StripeSettings(SingletonModel):
    """Stripe, and how the payment check is doing. The secret key is kept
    encrypted (core.secrets), never here."""

    enabled = models.BooleanField(
        "take payments through Stripe",
        default=False,
        help_text="Open invoices get a payment link, shown in the portal and in invoice emails.",
    )
    #: The payment check has read Stripe's completed checkouts up to here.
    checked_through = models.DateTimeField(null=True, blank=True, editable=False)
    last_check_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.TextField(blank=True, editable=False)
    last_error_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "Stripe settings"

    def __str__(self) -> str:
        return "Stripe settings"


class Provider(models.TextChoices):
    STRIPE = "stripe", "Stripe"


class PayLink(models.Model):
    """A link where the customer pays an invoice's balance. One is active per
    open invoice; a new balance, or a new portal address to come back to,
    makes a new one and turns the old one off."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    invoice = models.ForeignKey(
        "billing.Invoice", on_delete=models.PROTECT, related_name="pay_links"
    )
    provider = models.CharField(max_length=10, choices=Provider.choices, default=Provider.STRIPE)
    link_id = models.CharField(max_length=100, unique=True)
    url = models.URLField(max_length=500)
    amount = MoneyField()
    currency = models.CharField(max_length=3)
    #: Where the customer comes back to after paying, as it was when made.
    return_url = models.URLField(max_length=500, blank=True)
    test_mode = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    deactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["invoice", "active"])]

    def __str__(self) -> str:
        return f"{self.get_provider_display()} link for {self.invoice}"


class OnlinePayment(models.Model):
    """A payment made through a link, as the provider reported it, and the
    payment it was recorded as. Its checkout id is unique, so it's recorded
    once however often it's read."""

    class Status(models.TextChoices):
        WAITING = "waiting", "Being processed"
        RECORDED = "recorded", "Recorded"
        FAILED = "failed", "Not completed"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    provider = models.CharField(max_length=10, choices=Provider.choices, default=Provider.STRIPE)
    session_id = models.CharField(max_length=200, unique=True)
    link = models.ForeignKey(
        PayLink, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments"
    )
    invoice = models.ForeignKey(
        "billing.Invoice", on_delete=models.PROTECT, related_name="online_payments"
    )
    amount = MoneyField()
    currency = models.CharField(max_length=3)
    #: The provider's payment reference (a Stripe payment intent).
    reference = models.CharField(max_length=100, blank=True)
    email = models.CharField(max_length=254, blank=True)
    paid_at = models.DateTimeField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.WAITING)
    payment = models.ForeignKey(
        "billing.Payment", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    test_mode = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-paid_at"]

    def __str__(self) -> str:
        return f"{self.amount} online for {self.invoice}"
