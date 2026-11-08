"""Customers, contacts and addresses (FR-CUS-1..4)."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse

from trammel.core.models import BaseModel


class Customer(BaseModel):
    class Kind(models.TextChoices):
        INDIVIDUAL = "individual", "Individual"
        COMPANY = "company", "Company"

    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.INDIVIDUAL)
    name = models.CharField(
        max_length=200, help_text="The person's name, or the company's trading name."
    )
    company = models.CharField(
        "company / organization",
        max_length=200,
        blank=True,
        help_text="For an individual who buys on behalf of a company.",
    )
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    notes = models.TextField(blank=True)
    payment_terms_days = models.PositiveSmallIntegerField(
        "payment terms (days)",
        null=True,
        blank=True,
        help_text="Leave blank to use the business default.",
    )

    # Tax exemption (FR-CUS-2). The certificate document is attached through
    # the files module once it exists (FR-DOC-1).
    tax_exempt = models.BooleanField(default=False)
    tax_exempt_number = models.CharField(
        "exemption certificate / reseller permit", max_length=80, blank=True
    )
    tax_exempt_expires = models.DateField("exemption expires", null=True, blank=True)

    #: A stand-in for walk-ins and early proposals (FR-CUS-3). Jobs move from a
    #: placeholder to a real customer without losing their history.
    is_placeholder = models.BooleanField(default=False, editable=False)

    soft_delete_cascade = ("contacts", "addresses")
    customer_scope_path = "id"

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return (
            f"{self.name} ({self.company})"
            if self.company and self.company != self.name
            else self.name
        )

    def get_absolute_url(self) -> str:
        return reverse("customers:detail", args=[self.pk])

    def clean(self):
        if self.tax_exempt and not self.tax_exempt_number:
            raise ValidationError(
                {"tax_exempt_number": "A tax-exempt customer needs a certificate or permit number."}
            )

    def effective_payment_terms(self, business) -> int:
        return (
            self.payment_terms_days
            if self.payment_terms_days is not None
            else (business.default_payment_terms_days)
        )

    def exemption_valid_on(self, day) -> bool:
        return self.tax_exempt and (
            self.tax_exempt_expires is None or self.tax_exempt_expires >= day
        )

    def default_address(self, kind: str):
        flag = "is_default_billing" if kind == "billing" else "is_default_shipping"
        return self.addresses.filter(**{flag: True}).first() or self.addresses.first()

    @classmethod
    def search_filter(cls, term: str) -> models.Q:
        """Substring match on the identifying fields."""
        q = models.Q()
        for field in ("name", "company", "email", "phone"):
            q |= models.Q(**{f"{field}__icontains": term})
        return q

    @classmethod
    def placeholder(cls) -> Customer:
        """The walk-in placeholder, created on first use."""
        obj = cls.objects.filter(is_placeholder=True).order_by("created_at").first()
        if obj is None:
            obj = cls.objects.create(name="Walk-in customer", is_placeholder=True)
        return obj


class Contact(BaseModel):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="contacts")
    name = models.CharField(max_length=150)
    role = models.CharField("role / title", max_length=100, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    is_primary = models.BooleanField("primary contact", default=False)
    notes = models.CharField(max_length=300, blank=True)

    customer_scope_path = "customer_id"

    class Meta:
        ordering = ["-is_primary", "name"]

    def __str__(self) -> str:
        return self.name


class Address(BaseModel):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="addresses")
    label = models.CharField(max_length=60, blank=True, help_text="e.g. “Workshop”, “Head office”")
    line1 = models.CharField("address", max_length=200)
    line2 = models.CharField("address line 2", max_length=200, blank=True)
    city = models.CharField(max_length=100)
    region = models.CharField("state / province", max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, default="US", help_text="ISO country code")
    is_default_billing = models.BooleanField("default billing address", default=False)
    is_default_shipping = models.BooleanField("default shipping address", default=False)

    customer_scope_path = "customer_id"

    class Meta:
        ordering = ["-is_default_billing", "-is_default_shipping", "label"]
        verbose_name_plural = "addresses"

    def __str__(self) -> str:
        return self.label or self.line1

    def lines(self) -> list[str]:
        locality = " ".join(
            p
            for p in [self.city + ("," if self.region else ""), self.region, self.postal_code]
            if p
        )
        return [p for p in [self.line1, self.line2, locality, self.country] if p]
