"""Quotes with numbered revisions, lines, estimates and approvals (FR-QUO-1..11)."""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse

from trammel.core import units
from trammel.core.lifecycle import Lifecycle, LifecycleModel, Transition
from trammel.core.models import AppendOnlyModel, BaseModel, RushFeeKind, SingletonModel, uuid7
from trammel.core.money import MoneyField, PercentField, RateField

ZERO = Decimal("0")


class QuoteSettings(SingletonModel):
    validity_days = models.PositiveSmallIntegerField("quotes are valid for (days)", default=30)

    class Meta:
        verbose_name = "quote settings"

    def __str__(self) -> str:
        return "Quote settings"


class QuoteTemplate(BaseModel):
    """How a quote looks (FR-QUO-5). Sandboxed Jinja producing a full HTML page."""

    name = models.CharField(max_length=100, unique=True)
    body = models.TextField()
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-is_default", "name"]

    def __str__(self) -> str:
        return self.name

    @classmethod
    def default(cls) -> QuoteTemplate | None:
        return cls.objects.filter(is_active=True).order_by("-is_default", "name").first()


class Quote(BaseModel):
    """A quote, or a change order: the same priced document, told apart by kind.

    Anything priced for a job before it is approved is a quote. Once the job
    is approved the agreed scope exists, so anything priced after that is a
    change order (FR-WO-5): numbered in its own series, approved without the
    customer agreement (it may carry amended terms of its own), never moving
    the job's status, and flowing into the work order when approved.
    """

    class Kind(models.TextChoices):
        QUOTE = "quote", "Quote"
        CHANGE_ORDER = "change_order", "Change order"

    job = models.ForeignKey("jobs.Job", on_delete=models.PROTECT, related_name="quotes")
    kind = models.CharField(
        max_length=12, choices=Kind.choices, default=Kind.QUOTE, editable=False, db_index=True
    )
    number = models.CharField(max_length=32, unique=True, editable=False)
    title = models.CharField(max_length=200)

    customer_scope_path = "job__customer_id"
    soft_delete_cascade = ("revisions",)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.number} {self.title}"

    @property
    def is_change_order(self) -> bool:
        return self.kind == self.Kind.CHANGE_ORDER

    @property
    def noun(self) -> str:
        """“quote” or “change order”, for messages."""
        return self.get_kind_display().lower()

    def get_absolute_url(self) -> str:
        latest = self.latest
        return latest.get_absolute_url() if latest else reverse("quotes:list")

    @property
    def latest(self) -> QuoteRevision | None:
        return self.revisions.order_by("-number").first()


class Status(models.TextChoices):
    DRAFT = "draft", "Draft"
    SENT = "sent", "Sent"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"
    EXPIRED = "expired", "Expired"
    SUPERSEDED = "superseded", "Superseded"


def _has_lines(rev):
    if not rev.lines.exists():
        yield "Add at least one line before sending."
    if rev.valid_until is None:
        yield "Set the date the quote is valid until."


def _mark_sent(rev, transition, actor):
    from django.utils import timezone

    now = timezone.now()
    type(rev).all_objects.filter(pk=rev.pk).update(sent_at=now)
    rev.sent_at = now


REVISION_LIFECYCLE = Lifecycle(
    field="status",
    initial=Status.DRAFT,
    transitions=[
        Transition(
            Status.DRAFT,
            Status.SENT,
            permission="quotes.edit",
            label="Mark as sent",
            guards=(_has_lines,),
            after=(_mark_sent,),
        ),
        # Quotes and change orders are approved under different permissions;
        # services.approve checks the right one.
        Transition(Status.SENT, Status.APPROVED, label="Approve"),
        Transition(
            Status.SENT,
            Status.DECLINED,
            permission=("quotes.edit", "quotes.respond"),
            label="Declined",
        ),
        Transition(Status.SENT, Status.EXPIRED, permission="quotes.edit", label="Expire"),
        Transition(
            (Status.DRAFT, Status.SENT),
            Status.SUPERSEDED,
            permission="quotes.edit",
            label="Supersede",
        ),
    ],
)


class QuoteRevision(LifecycleModel, BaseModel):
    """One numbered version of a quote. Immutable once sent (FR-QUO-6)."""

    class Deposit(models.TextChoices):
        NONE = "none", "No deposit"
        PERCENT = "percent", "Percentage of the total"
        FIXED = "fixed", "Fixed amount"

    class Fulfillment(models.TextChoices):
        PICKUP = "pickup", "Pickup at the shop"
        DELIVERY = "delivery", "Delivery"
        SHIPPING = "shipping", "Shipping"

    quote = models.ForeignKey(Quote, on_delete=models.PROTECT, related_name="revisions")
    number = models.PositiveIntegerField(default=1)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, editable=False
    )
    template = models.ForeignKey(QuoteTemplate, null=True, blank=True, on_delete=models.PROTECT)
    valid_until = models.DateField(null=True, blank=True)
    #: What is being done, in narrative terms: details a list of lines doesn't
    #: capture. Shown near the top of the document.
    description = models.TextField(
        blank=True,
        help_text="What is being done, in your own words: details the lines don't capture. "
        "Shown on the document above the lines.",
    )
    customer_notes = models.TextField("notes for the customer", blank=True)
    internal_notes = models.TextField(blank=True)
    #: Change orders only (FR-AGR-6): changed terms the customer accepts with
    #: the change order. Blank means the job's agreement stands as it is.
    amended_terms = models.TextField(
        blank=True,
        help_text="Changed terms the customer accepts with this change order (optional).",
    )
    discount_pct = PercentField("quote discount (%)", default=ZERO)
    deposit_kind = models.CharField(max_length=8, choices=Deposit.choices, default=Deposit.NONE)
    deposit_value = models.DecimalField(
        "deposit (% or amount)", max_digits=12, decimal_places=2, default=ZERO
    )
    fulfillment = models.CharField(
        max_length=10,
        choices=Fulfillment.choices,
        default=Fulfillment.PICKUP,
        help_text="Decides which address the tax estimate uses.",
    )
    shipping = MoneyField(
        "shipping / delivery charge",
        default=ZERO,
        validators=[MinValueValidator(0)],
        help_text="Shown as its own line under the subtotal, and carried to the invoice.",
    )

    # Rates used to price this revision — copied from Shop rates when the
    # revision is created, and overridable per quote (FR-QUO-3).
    labor_rate = RateField(
        "labor rate (per hour)",
        default=ZERO,
        validators=[MinValueValidator(0)],
        help_text="What this quote charges for an hour of work. Starts from your shop rates.",
    )
    overhead_rate = RateField(
        "overhead (per hour)",
        default=ZERO,
        validators=[MinValueValidator(0)],
        help_text="Added to each hour of labor and machine time on this quote. Starts from "
        "your shop rates.",
    )
    material_markup = PercentField(
        "material markup (%)",
        default=ZERO,
        help_text="Added to material costs to price them on this quote, unless a material has "
        "its own markup. 50 prices a $10 material at $15.",
    )
    consumables_pct = PercentField(
        "consumables allowance (%)",
        default=ZERO,
        help_text="Supplies a job uses up, as a percentage added to the cost of materials on "
        "this quote.",
    )
    minimum_charge = MoneyField(
        default=ZERO,
        help_text="The least this quote comes to; lines adding up to less are raised to it. "
        "0 for no minimum.",
    )
    #: The rush fee (0.8.2), from Shop rates: on a rush job's quote, a line
    #: worth a percent of the other lines or a flat amount, never under the minimum.
    rush_fee_kind = models.CharField(
        "rush fee", max_length=8, choices=RushFeeKind.choices, default=RushFeeKind.PERCENT
    )
    rush_fee_value = models.DecimalField(
        "rush fee (percent or amount)",
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(0)],
        help_text="The rush fee line's percent of the other lines, or its flat amount. "
        "Starts from your shop rates.",
    )
    rush_fee_minimum = MoneyField(
        "least rush fee",
        default=ZERO,
        validators=[MinValueValidator(0)],
        help_text="The rush fee is never less than this (when there are other lines). 0 for none.",
    )
    rates_overridden = models.BooleanField(default=False, editable=False)

    # Totals, recomputed whenever a draft changes and frozen once sent.
    lines_total = MoneyField(default=ZERO, editable=False)
    discount_amount = MoneyField(default=ZERO, editable=False)
    minimum_adjustment = MoneyField(default=ZERO, editable=False)
    subtotal = MoneyField(default=ZERO, editable=False)
    #: The part of `shipping` that the tax estimate includes.
    shipping_taxable = MoneyField(default=ZERO, editable=False)
    tax_label = models.CharField(max_length=120, blank=True, editable=False)
    tax_amount = MoneyField(default=ZERO, editable=False)
    total = MoneyField(default=ZERO, editable=False)
    cost_total = MoneyField(default=ZERO, editable=False)

    sent_at = models.DateTimeField(null=True, blank=True, editable=False)

    lifecycle = REVISION_LIFECYCLE
    customer_scope_path = "quote__job__customer_id"

    #: Fields that may change after the revision has been sent.
    _MUTABLE_AFTER_SEND = frozenset({"status", "sent_at", "updated_at", "deleted_at"})

    class Meta:
        ordering = ["quote", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["quote", "number"], name="unique_quote_revision")
        ]

    def __str__(self) -> str:
        return f"{self.quote.number} rev {self.number}"

    @property
    def never_sent(self) -> bool:
        return self.status == Status.DRAFT and self.sent_at is None

    def get_absolute_url(self) -> str:
        return reverse("quotes:revision", args=[self.pk])

    @property
    def is_draft(self) -> bool:
        return self.status == Status.DRAFT

    @property
    def shipping_label(self) -> str:
        return "Delivery" if self.fulfillment == self.Fulfillment.DELIVERY else "Shipping"

    @property
    def has_choices(self) -> bool:
        """Whether it offers options or alternatives (FR-QUO-11)."""
        return self.lines.exclude(offer=QuoteLine.Offer.INCLUDED).exists()

    def included_lines(self):
        """The lines in its scope and totals: every included line, the chosen
        alternative of each group, and the options added. Before approval the
        defaults; once approved, what the customer chose."""
        return self.lines.filter(included=True)

    @property
    def deposit_amount(self) -> Decimal:
        return self.deposit_for(self.total)

    def deposit_for(self, total: Decimal) -> Decimal:
        """The deposit on `total`, such as the total with the customer's choices."""
        from trammel.core.money import round_money

        if total <= 0:  # a change order that credits the customer
            return ZERO
        if self.deposit_kind == self.Deposit.PERCENT:
            return round_money(total * self.deposit_value / 100)
        if self.deposit_kind == self.Deposit.FIXED:
            return min(self.deposit_value, total)
        return ZERO

    def save(self, *args, **kwargs):
        if not self._state.adding and getattr(self, "_loaded_state", Status.DRAFT) != Status.DRAFT:
            fields = kwargs.get("update_fields")
            if not getattr(self, "_lifecycle_permit", False) and (
                fields is None or not set(fields) <= self._MUTABLE_AFTER_SEND
            ):
                raise ValidationError(
                    "A sent quote revision cannot be changed. Create a new revision instead."
                )
        super().save(*args, **kwargs)


class Category(models.TextChoices):
    """For quoted-versus-actual job costing (FR-JOB-3)."""

    MATERIALS = "materials", "Materials"
    LABOR = "labor", "Labor"
    MACHINE = "machine", "Machine time"
    OTHER = "other", "Other"


class QuoteLine(models.Model):
    class Offer(models.TextChoices):
        """How a line is offered (FR-QUO-11)."""

        INCLUDED = "included", "Included"
        OPTIONAL = "optional", "Optional: the customer may add it"
        ALTERNATE = "alternate", "An alternative: the customer picks one of a group"

    class Kind(models.TextChoices):
        PRODUCT = "product", "Catalog product"
        ADHOC = "adhoc", "Item"
        LABOR = "labor", "Labor"
        MACHINE = "machine", "Machine time"
        MATERIAL = "material", "Material"
        ESTIMATE = "estimate", "Estimate"
        RUSH = "rush", "Rush fee"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    revision = models.ForeignKey(QuoteRevision, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField(default=0)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    description = models.TextField()
    quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("1"))
    unit = models.CharField(max_length=12, choices=units.UNIT_CHOICES, default="each")
    unit_price = RateField(default=ZERO)
    #: Set when the price was entered by hand instead of coming from the line's
    #: source (catalog product, rates, material, estimate): the price that
    #: source gave, kept for comparison. unit_price holds the manual price.
    calculated_unit_price = RateField(null=True, blank=True)
    #: Why the price was set by hand. Internal: never on customer documents.
    price_note = models.CharField("reason for the manual price", max_length=300, blank=True)
    unit_cost = RateField(default=ZERO)
    discount_pct = PercentField("line discount (%)", default=ZERO)
    line_total = MoneyField(default=ZERO)
    cost_total = MoneyField(default=ZERO)
    tax_category = models.ForeignKey(
        "tax.TaxCategory", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    product = models.ForeignKey(
        "catalog.Product", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    machine = models.ForeignKey(
        "shop.Machine", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    material = models.ForeignKey(
        "shop.Material", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    estimator = models.CharField(max_length=32, blank=True)
    inputs = models.JSONField(default=dict, blank=True)
    #: The visible cost breakdown (FR-QUO-2): rows of {label, detail, cost, price}.
    breakdown = models.JSONField(default=list, blank=True)
    #: line_total split by Category, for job costing.
    split = models.JSONField(default=dict, blank=True)
    offer = models.CharField(max_length=10, choices=Offer.choices, default=Offer.INCLUDED)
    #: An alternative's group, such as “Wood”: the customer picks one line of it.
    group = models.CharField(max_length=80, blank=True)
    #: In the scope and totals. Always for an included line; for an option, once
    #: added; for an alternative, when it's its group's choice (the default one
    #: until the customer picks). Set by the services, never by hand.
    included = models.BooleanField(default=True, editable=False)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.description[:60]

    @property
    def price_is_manual(self) -> bool:
        return self.calculated_unit_price is not None

    @property
    def is_optional(self) -> bool:
        return self.offer == self.Offer.OPTIONAL

    @property
    def is_alternate(self) -> bool:
        return self.offer == self.Offer.ALTERNATE

    def _guard(self):
        status = (
            QuoteRevision.all_objects.filter(pk=self.revision_id)
            .values_list("status", flat=True)
            .first()
        )
        if status != Status.DRAFT:
            raise ValidationError("Lines of a sent quote revision cannot be changed.")

    def save(self, *args, **kwargs):  # noqa: DJ012 — _guard reads better first
        self._guard()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._guard()
        return super().delete(*args, **kwargs)


class Approval(AppendOnlyModel):
    """Who approved what, when and how — with snapshots (FR-QUO-8, §6 step 5)."""

    class Method(models.TextChoices):
        MANUAL = "manual", "Recorded by staff"
        PORTAL = "portal", "Customer portal"
        SIGNED = "signed", "Signed in the shop"
        ONLINE = "online", "Paid online"

    class How(models.TextChoices):
        IN_PERSON = "in_person", "In person"
        PHONE = "phone", "By phone"
        EMAIL = "email", "By email"
        OTHER = "other", "Other"

    revision = models.OneToOneField(
        QuoteRevision, on_delete=models.PROTECT, related_name="approval"
    )
    method = models.CharField(max_length=8, choices=Method.choices, default=Method.MANUAL)
    how = models.CharField(max_length=10, choices=How.choices, blank=True)
    approved_by_name = models.CharField(max_length=150)
    approved_at = models.DateTimeField()
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    notes = models.TextField(blank=True)
    #: The customer's drawn signature, as a cropped PNG, when they signed.
    signature = models.BinaryField(blank=True, default=b"", editable=False)
    snapshot = models.JSONField(default=dict)
    agreement = models.ForeignKey(
        "agreements.CustomerAgreement",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    agreement_text = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"Approval of {self.revision}"
