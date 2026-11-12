"""Invoices, payments, credit notes and write-offs (FR-BIL-1..6)."""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from trammel.core import units
from trammel.core.lifecycle import Lifecycle, LifecycleModel, Transition
from trammel.core.models import AppendOnlyModel, BaseModel, uuid7
from trammel.core.money import MoneyField, PercentField, RateField

ZERO = Decimal("0")


class InvoiceTemplate(BaseModel):
    name = models.CharField(max_length=100, unique=True)
    body = models.TextField()
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-is_default", "name"]

    def __str__(self) -> str:
        return self.name

    @classmethod
    def default(cls):
        return cls.objects.filter(is_active=True).order_by("-is_default", "name").first()


class Status(models.TextChoices):
    DRAFT = "draft", "Draft"
    ISSUED = "issued", "Issued"
    PARTIALLY_PAID = "partially_paid", "Partially paid"
    PAID = "paid", "Paid"
    VOID = "void", "Void"


OPEN = (Status.ISSUED, Status.PARTIALLY_PAID)

INVOICE_LIFECYCLE = Lifecycle(
    field="status",
    initial=Status.DRAFT,
    transitions=[
        # Issue and void go through services, which allocate the number and
        # check payments first; the rest follow payments automatically.
        Transition(Status.DRAFT, Status.ISSUED, permission="billing.issue", label="Issue"),
        Transition((Status.ISSUED, Status.PARTIALLY_PAID), Status.PAID, label="Paid"),
        Transition((Status.ISSUED, Status.PAID), Status.PARTIALLY_PAID, label="Partially paid"),
        Transition((Status.PARTIALLY_PAID, Status.PAID), Status.ISSUED, label="Unpaid"),
        Transition(
            Status.ISSUED,
            Status.VOID,
            permission="billing.void",
            requires_reason=True,
            label="Void",
        ),
    ],
)


class Invoice(LifecycleModel, BaseModel):
    class Kind(models.TextChoices):
        DEPOSIT = "deposit", "Deposit invoice"
        PROGRESS = "progress", "Progress invoice"
        FINAL = "final", "Final invoice"
        DIRECT_SALE = "direct_sale", "Sale / receipt"
        CREDIT_NOTE = "credit_note", "Credit note"

    class Fulfillment(models.TextChoices):
        PICKUP = "pickup", "Pickup at the shop"
        DELIVERY = "delivery", "Delivery"
        SHIPPING = "shipping", "Shipping"

    class TaxMethod(models.TextChoices):
        ADDRESS = "address", "Looked up from the address"
        MANUAL = "manual", "Chosen by hand"
        EXEMPT = "exempt", "Customer exempt"
        NONE = "none", "No sales tax"

    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.FINAL)
    number = models.CharField(max_length=32, blank=True, editable=False)
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="invoices"
    )
    job = models.ForeignKey(
        "jobs.Job", null=True, blank=True, on_delete=models.PROTECT, related_name="invoices"
    )
    credit_for = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="credit_notes"
    )
    source_revision = models.ForeignKey(
        "quotes.QuoteRevision",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoices",
    )
    template = models.ForeignKey(InvoiceTemplate, null=True, blank=True, on_delete=models.PROTECT)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT, editable=False
    )

    invoice_date = models.DateField(
        null=True, blank=True, help_text="Leave blank to use the day it is issued."
    )
    terms_days = models.PositiveSmallIntegerField("payment terms (days)", default=30)
    due_date = models.DateField(null=True, blank=True, editable=False)
    fulfillment = models.CharField(
        max_length=10, choices=Fulfillment.choices, default=Fulfillment.PICKUP
    )
    ship_to = models.ForeignKey(
        "customers.Address",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="For delivery or shipping: decides the tax location.",
    )
    ship_to_text = models.TextField(blank=True, editable=False)
    shipping = MoneyField(
        "shipping / delivery charge",
        default=ZERO,
        validators=[MinValueValidator(0)],
        help_text="Shown as its own line under the subtotal.",
    )
    notes = models.TextField("notes for the customer", blank=True)
    internal_notes = models.TextField(blank=True)

    # Tax as determined when issued — a snapshot that later imports never change (FR-TAX-7).
    manual_jurisdiction = models.ForeignKey(
        "tax.Jurisdiction",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text="Only when the address cannot be resolved.",
    )
    tax_method = models.CharField(
        max_length=8, choices=TaxMethod.choices, blank=True, editable=False
    )
    tax_location_code = models.CharField(max_length=40, blank=True, editable=False)
    tax_postal_code = models.CharField(max_length=10, blank=True, editable=False)
    tax_rate_total = models.DecimalField(
        max_digits=8, decimal_places=4, default=ZERO, editable=False
    )
    tax_flagged = models.BooleanField("tax needs review", default=False, editable=False)
    tax_note = models.CharField(max_length=300, blank=True, editable=False)
    #: A marketplace facilitator (Etsy) that collected this sale's sales tax and
    #: files and pays it itself: the tax is on the sale, but not owed by the shop.
    tax_remitted_by = models.CharField(
        "sales tax paid by", max_length=60, blank=True, editable=False
    )
    exemption_number = models.CharField(max_length=80, blank=True, editable=False)

    subtotal = MoneyField(default=ZERO, editable=False)
    #: The part of `shipping` included in `taxable_total`.
    shipping_taxable = MoneyField(default=ZERO, editable=False)
    taxable_total = MoneyField(default=ZERO, editable=False)
    tax_total = MoneyField(default=ZERO, editable=False)
    total = MoneyField(default=ZERO, editable=False)
    paid_total = MoneyField(default=ZERO, editable=False)
    credited_total = MoneyField(default=ZERO, editable=False)
    written_off = MoneyField(default=ZERO, editable=False)
    balance = MoneyField(default=ZERO, editable=False)

    issued_at = models.DateTimeField(null=True, blank=True, editable=False)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
    )
    voided_at = models.DateTimeField(null=True, blank=True, editable=False)

    lifecycle = INVOICE_LIFECYCLE
    customer_scope_path = "customer_id"

    #: Fields services may write after issue (status and money roll-ups).
    _MUTABLE_AFTER_ISSUE = frozenset(
        {
            "status",
            "paid_total",
            "credited_total",
            "written_off",
            "balance",
            "voided_at",
            "updated_at",
        }
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["number"], condition=~Q(number=""), name="unique_invoice_number"
            ),
        ]
        indexes = [
            models.Index(fields=["status", "due_date"]),
            models.Index(fields=["customer", "status"]),
        ]

    def __str__(self) -> str:
        return self.number or f"Draft {self.get_kind_display().lower()}"

    def get_absolute_url(self) -> str:
        return reverse("billing:detail", args=[self.pk])

    @property
    def is_draft(self) -> bool:
        return self.status == Status.DRAFT

    @property
    def is_credit_note(self) -> bool:
        return self.kind == self.Kind.CREDIT_NOTE

    @property
    def shipping_label(self) -> str:
        return "Delivery" if self.fulfillment == self.Fulfillment.DELIVERY else "Shipping"

    @property
    def tax_label(self) -> str:
        """One combined line for customers; the per-jurisdiction detail is in `taxes`."""
        from trammel.tax.services import rate_text

        if self.tax_method == self.TaxMethod.EXEMPT:
            return "Sales tax (exempt)"
        if not self.tax_rate_total:
            return "Sales tax"
        return f"Sales tax ({rate_text(self.tax_rate_total)})"

    @property
    def is_overdue(self) -> bool:
        """Computed from the due date, never stored (§6.1)."""
        return (
            self.status in OPEN
            and self.due_date is not None
            and self.due_date < timezone.localdate()
        )

    @property
    def days_overdue(self) -> int:
        return (timezone.localdate() - self.due_date).days if self.is_overdue else 0

    def save(self, *args, **kwargs):
        if not self._state.adding and getattr(self, "_loaded_state", Status.DRAFT) != Status.DRAFT:
            fields = kwargs.get("update_fields")
            if not getattr(self, "_lifecycle_permit", False) and (
                fields is None or not set(fields) <= self._MUTABLE_AFTER_ISSUE
            ):
                raise ValidationError(
                    "An issued invoice cannot be changed. Void it or issue a credit note."
                )
        super().save(*args, **kwargs)


class Category(models.TextChoices):
    MATERIALS = "materials", "Materials"
    LABOR = "labor", "Labor"
    MACHINE = "machine", "Machine time"
    PRODUCT = "product", "Products"
    DEPOSIT = "deposit", "Deposit"
    OTHER = "other", "Other"


class InvoiceLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField(default=0)
    description = models.TextField()
    quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("1"))
    unit = models.CharField(max_length=12, choices=units.UNIT_CHOICES, default="each")
    unit_price = RateField(default=ZERO)
    discount_pct = PercentField("discount (%)", default=ZERO)
    line_total = MoneyField(default=ZERO)
    category = models.CharField(max_length=10, choices=Category.choices, default=Category.OTHER)
    tax_category = models.ForeignKey(
        "tax.TaxCategory", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    product = models.ForeignKey(
        "catalog.Product", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Snapshots taken at issue (FR-TAX-2, -3).
    taxable = models.BooleanField(default=True)
    gross_receipts_classification = models.CharField(max_length=60, blank=True)
    #: Where the line came from, so the same work is never billed twice (FR-BIL-1).
    source_type = models.CharField(max_length=24, blank=True)
    source_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["position", "id"]
        indexes = [models.Index(fields=["source_type", "source_id"])]

    def __str__(self) -> str:
        return self.description[:60]

    def _guard(self):
        status = (
            Invoice.all_objects.filter(pk=self.invoice_id).values_list("status", flat=True).first()
        )
        if status not in (None, Status.DRAFT):
            raise ValidationError("Lines of an issued invoice cannot be changed.")

    def save(self, *args, **kwargs):  # noqa: DJ012 — _guard reads better first
        self._guard()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._guard()
        return super().delete(*args, **kwargs)


class InvoiceTax(models.Model):
    """One rate's tax on an issued invoice — code, rate, base and amount (FR-TAX-7)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="taxes")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    rate = models.DecimalField(max_digits=8, decimal_places=4)
    base = MoneyField()
    amount = MoneyField()

    def __str__(self) -> str:
        return f"{self.code} {self.rate}%: {self.amount}"


class Payment(AppendOnlyModel):
    """Money received outside Trammel, recorded here (§1.2, FR-BIL-4). Never edited."""

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        CHECK = "check", "Check"
        CARD = "card", "Card"
        BANK = "bank", "Bank transfer"
        ONLINE = "online", "Online store"
        #: Paid by the customer through a Stripe payment link (online payments).
        STRIPE = "stripe", "Stripe"
        OTHER = "other", "Other"

    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="payments"
    )
    received_on = models.DateField()
    amount = MoneyField(help_text="Negative records a refund.")
    method = models.CharField(max_length=8, choices=Method.choices)
    reference = models.CharField(max_length=100, blank=True, help_text="Check number, etc.")
    notes = models.CharField(max_length=300, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    customer_scope_path = "customer_id"

    class Meta:
        ordering = ["-received_on", "-created_at"]

    def __str__(self) -> str:
        return f"{self.amount} {self.get_method_display()} on {self.received_on}"


class Allocation(AppendOnlyModel):
    """Part of a payment or a credit note applied to an invoice. Negative reverses."""

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="allocations")
    payment = models.ForeignKey(
        Payment, null=True, blank=True, on_delete=models.PROTECT, related_name="allocations"
    )
    credit_note = models.ForeignKey(
        Invoice, null=True, blank=True, on_delete=models.PROTECT, related_name="allocations_made"
    )
    amount = MoneyField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(payment__isnull=False, credit_note__isnull=True)
                | Q(payment__isnull=True, credit_note__isnull=False),
                name="allocation_has_one_source",
            )
        ]

    def __str__(self) -> str:
        return f"{self.amount} → {self.invoice}"


class WriteOff(AppendOnlyModel):
    """An amount the shop will not collect (§6.1). A bad-debt deduction (FR-TAX-5)."""

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="write_offs")
    amount = MoneyField()
    written_off_on = models.DateField(default=timezone.localdate)
    reason = models.TextField()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    from_force_closeout = models.BooleanField(default=False)

    class Meta:
        ordering = ["-written_off_on"]

    def __str__(self) -> str:
        return f"{self.amount} written off {self.invoice}"
