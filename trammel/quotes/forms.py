from __future__ import annotations

from decimal import Decimal

from django import forms
from django.db.models import Q

from trammel.catalog.models import Product
from trammel.core import units
from trammel.core.money import MoneyInput
from trammel.shop.models import Machine, Material
from trammel.tax.models import TaxCategory

from .models import ZERO, Approval, QuoteLine, QuoteRevision, QuoteSettings, QuoteTemplate

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


def _qty(label="Quantity", **kw):
    return forms.DecimalField(
        label=label, max_digits=12, decimal_places=4, min_value=Decimal("0.0001"), initial=1, **kw
    )


def _pct(label="Discount (%)"):
    return forms.DecimalField(
        label=label,
        max_digits=7,
        decimal_places=3,
        min_value=0,
        max_value=100,
        initial=0,
        required=False,
    )


class RevisionForm(forms.ModelForm):
    class Meta:
        model = QuoteRevision
        fields = [
            "valid_until",
            "fulfillment",
            "shipping",
            "discount_pct",
            "deposit_kind",
            "deposit_value",
            "template",
            "customer_notes",
            "amended_terms",
            "internal_notes",
            "labor_rate",
            "overhead_rate",
            "material_markup",
            "consumables_pct",
            "minimum_charge",
            "rush_fee_kind",
            "rush_fee_value",
            "rush_fee_minimum",
        ]
        widgets = {
            "valid_until": DATE,
            "customer_notes": forms.Textarea(attrs={"rows": 3}),
            "amended_terms": forms.Textarea(attrs={"rows": 4}),
            "internal_notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.quote_id and self.instance.quote.is_change_order:
            # The job's minimum charge was met by its quote.
            del self.fields["minimum_charge"]
            self.fields["deposit_kind"].help_text = "A deposit on this change order, if any."
        else:
            del self.fields["amended_terms"]
        self.fields["template"].queryset = QuoteTemplate.objects.filter(
            Q(is_active=True) | Q(pk=self.instance.template_id)
        )

    def clean(self):
        data = super().clean()
        if data.get("deposit_kind") == "percent" and (data.get("deposit_value") or 0) > 100:
            self.add_error("deposit_value", "A percentage deposit cannot exceed 100.")
        return data


class ManualPriceFields(forms.Form):
    """A price entered by hand in place of the calculated one, with the reason.

    It replaces the line's unit price without touching the catalog product, the
    rates or a discount, and without making a one-off item.
    """

    manual_price = MoneyInput(
        label="Manual unit price",
        max_digits=14,
        decimal_places=4,
        min_value=0,
        required=False,
        help_text="Leave blank to use the calculated price.",
    )
    price_note = forms.CharField(
        label="Reason for the manual price",
        max_length=300,
        required=False,
        help_text="Needed with a manual price. Internal: customers never see it.",
    )

    def clean(self):
        data = super().clean()
        if data.get("manual_price") is not None and not (data.get("price_note") or "").strip():
            self.add_error("price_note", "Say why the price was set by hand.")
        return data

    def apply_manual_price(self, line: QuoteLine) -> QuoteLine:
        from .services import apply_manual_price

        return apply_manual_price(
            line, self.cleaned_data.get("manual_price"), self.cleaned_data.get("price_note", "")
        )

    @staticmethod
    def manual_initial(line: QuoteLine | None) -> dict:
        if line is None or not line.price_is_manual:
            return {}
        return {"manual_price": line.unit_price, "price_note": line.price_note}


class _LineForm(ManualPriceFields):
    discount_pct = _pct()

    #: An Item's price is typed in anyway, so it offers no separate manual price.
    manual_price_allowed = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tail = ["discount_pct", "manual_price", "price_note"]
        if not self.manual_price_allowed:
            del self.fields["manual_price"], self.fields["price_note"]
            tail = ["discount_pct"]
        self.order_fields([f for f in self.fields if f not in tail] + tail)

    def make(self, revision: QuoteRevision, line: QuoteLine | None = None) -> QuoteLine:
        """The line priced from its source (product, rates, material)."""
        raise NotImplementedError

    def build(self, revision: QuoteRevision, line: QuoteLine | None = None) -> QuoteLine:
        line = self.make(revision, line)
        if not self.manual_price_allowed:
            line.calculated_unit_price, line.price_note = None, ""
            return line
        return self.apply_manual_price(line)

    @staticmethod
    def initial_from(line: QuoteLine) -> dict:
        return {
            "description": line.description,
            "quantity": line.quantity,
            "discount_pct": line.discount_pct,
            **ManualPriceFields.manual_initial(line),
        }


class ProductLineForm(_LineForm):
    product = forms.ModelChoiceField(
        Product.objects.filter(is_active=True), required=False, empty_label="Choose a product"
    )
    # A product not in the catalog yet, added to it with the line. The view
    # removes these for anyone who may not edit the catalog.
    new_name = forms.CharField(label="Name", max_length=200, required=False)
    new_sku = forms.CharField(label="SKU", max_length=64, required=False)
    new_kind = forms.ChoiceField(
        label="Kind", choices=Product.Kind.choices, initial="made", required=False
    )
    new_price = MoneyInput(
        label="Price", max_digits=12, decimal_places=2, min_value=0, required=False
    )
    new_cost = MoneyInput(
        label="Cost",
        max_digits=14,
        decimal_places=4,
        min_value=0,
        required=False,
        help_text="What one unit costs you, for margins.",
    )
    new_unit = forms.ChoiceField(
        label="Unit", choices=units.UNIT_CHOICES, initial="each", required=False
    )
    new_tax_category = forms.ModelChoiceField(
        TaxCategory.objects.all(),
        label="Tax category",
        required=False,
        help_text="Leave blank for the default category.",
    )
    quantity = _qty()
    description = forms.CharField(required=False, help_text="Leave blank for the product name.")

    NEW_FIELDS = (
        "new_name",
        "new_sku",
        "new_kind",
        "new_price",
        "new_cost",
        "new_unit",
        "new_tax_category",
    )

    def drop_new_product(self) -> None:
        for name in self.NEW_FIELDS:
            self.fields.pop(name, None)

    @property
    def offers_new_product(self) -> bool:
        return "new_name" in self.fields

    @property
    def new_product_fields(self):
        return [self[name] for name in self.NEW_FIELDS if name in self.fields]

    @property
    def new_product_open(self) -> bool:
        return bool(self.data.get("new_name")) or any(f.errors for f in self.new_product_fields)

    @property
    def wants_new_product(self) -> bool:
        return self.offers_new_product and not self.cleaned_data.get("product")

    def clean(self):
        data = super().clean()
        if data.get("product"):
            if data.get("new_name"):
                raise forms.ValidationError(
                    "Choose a product from the catalog or add a new one, not both."
                )
        elif not self.offers_new_product:
            self.add_error("product", "Choose a product.")
        elif not data.get("new_name"):
            raise forms.ValidationError("Choose a product, or add a new one below.")
        elif data.get("new_price") is None:
            self.add_error("new_price", "A new product needs a price.")
        return data

    def new_product(self) -> dict:
        """The new product's fields, for catalog.services.create_product."""
        d = self.cleaned_data
        return {
            "name": d["new_name"],
            "sku": d["new_sku"],
            "kind": d["new_kind"] or Product.Kind.MADE,
            "price": d["new_price"],
            "cost": d["new_cost"] or ZERO,
            "unit": d["new_unit"] or "each",
            "tax_category": d["new_tax_category"],
        }

    def make(self, revision, line=None):
        d = self.cleaned_data
        p = d["product"]
        line = line or QuoteLine(revision=revision, kind=QuoteLine.Kind.PRODUCT)
        line.product = p
        line.description = d["description"] or p.name
        line.quantity, line.unit = d["quantity"], p.unit
        line.unit_price, line.unit_cost = p.price, p.cost
        line.tax_category = p.tax_category
        line.discount_pct = d["discount_pct"] or ZERO
        return line


class AdhocLineForm(_LineForm):
    manual_price_allowed = False

    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))
    quantity = _qty()
    unit = forms.ChoiceField(choices=units.UNIT_CHOICES, initial="each")
    unit_price = MoneyInput(max_digits=14, decimal_places=4, min_value=0)
    unit_cost = MoneyInput(
        max_digits=14,
        decimal_places=4,
        min_value=0,
        required=False,
        initial=0,
        help_text="Your cost, for margins.",
    )
    tax_category = forms.ModelChoiceField(TaxCategory.objects.all(), required=False)
    save_to_catalog = forms.BooleanField(
        required=False, help_text="Also add this item to the product catalog."
    )

    def allow_credit(self) -> None:
        """On a change order, a negative price credits removed scope."""
        self.fields["unit_price"] = MoneyInput(
            max_digits=14,
            decimal_places=4,
            help_text="A negative price is a credit for scope taken out.",
        )
        self.fields.pop("save_to_catalog", None)

    def make(self, revision, line=None):
        d = self.cleaned_data
        line = line or QuoteLine(revision=revision, kind=QuoteLine.Kind.ADHOC)
        line.description, line.quantity, line.unit = d["description"], d["quantity"], d["unit"]
        line.unit_price, line.unit_cost = d["unit_price"], d["unit_cost"] or ZERO
        line.tax_category = d["tax_category"]
        line.discount_pct = d["discount_pct"] or ZERO
        return line

    @staticmethod
    def initial_from(line):
        return {
            **_LineForm.initial_from(line),
            "unit": line.unit,
            "unit_price": line.unit_price,
            "unit_cost": line.unit_cost,
            "tax_category": line.tax_category_id,
        }


class LaborLineForm(_LineForm):
    description = forms.CharField(initial="Labor")
    quantity = _qty("Hours")

    def make(self, revision, line=None):
        d = self.cleaned_data
        line = line or QuoteLine(revision=revision, kind=QuoteLine.Kind.LABOR)
        line.description, line.quantity, line.unit = d["description"], d["quantity"], "hr"
        line.unit_price = line.unit_cost = revision.labor_rate + revision.overhead_rate
        line.discount_pct = d["discount_pct"] or ZERO
        return line


class MachineLineForm(_LineForm):
    machine = forms.ModelChoiceField(Machine.objects.filter(is_active=True))
    quantity = _qty("Hours")
    description = forms.CharField(required=False)

    def make(self, revision, line=None):
        d = self.cleaned_data
        m = d["machine"]
        line = line or QuoteLine(revision=revision, kind=QuoteLine.Kind.MACHINE)
        line.machine = m
        line.description = d["description"] or f"Machine time — {m}"
        line.quantity, line.unit = d["quantity"], "hr"
        line.unit_price = m.hourly_rate + revision.overhead_rate
        line.unit_cost = m.cost_rate + revision.overhead_rate
        line.discount_pct = d["discount_pct"] or ZERO
        return line

    @staticmethod
    def initial_from(line):
        return {**_LineForm.initial_from(line), "machine": line.machine_id}


class MaterialLineForm(_LineForm):
    material = forms.ModelChoiceField(Material.objects.filter(is_active=True))
    quantity = _qty()
    unit = forms.ChoiceField(
        choices=[("", "— material's own unit —"), *units.UNIT_CHOICES], required=False
    )
    description = forms.CharField(required=False)

    def clean(self):
        data = super().clean()
        material, unit = data.get("material"), data.get("unit")
        if material and unit:
            try:
                material.cost_per(unit)
            except units.IncompatibleUnits as exc:
                self.add_error("unit", str(exc))
        return data

    def make(self, revision, line=None):
        d = self.cleaned_data
        m = d["material"]
        unit = d["unit"] or m.unit
        cost = m.cost_per(unit) * (1 + revision.consumables_pct / 100)
        markup = m.markup if m.markup is not None else revision.material_markup
        line = line or QuoteLine(revision=revision, kind=QuoteLine.Kind.MATERIAL)
        line.material = m
        line.description = d["description"] or str(m)
        line.quantity, line.unit = d["quantity"], unit
        line.unit_cost = cost.quantize(Decimal("0.0001"))
        line.unit_price = (cost * (1 + markup / 100)).quantize(Decimal("0.0001"))
        line.discount_pct = d["discount_pct"] or ZERO
        return line

    @staticmethod
    def initial_from(line):
        return {**_LineForm.initial_from(line), "material": line.material_id, "unit": line.unit}


class RushLineForm(_LineForm):
    """A rush fee (0.8.2): worked out from the quote's rush rates and its other
    lines, and kept current as they change, unless a price is typed in."""

    description = forms.CharField(required=False, help_text="Blank for “Rush fee”.")

    def make(self, revision, line=None):
        from .services import rush_amount, rush_label

        d = self.cleaned_data
        line = line or QuoteLine(revision=revision, kind=QuoteLine.Kind.RUSH)
        line.description = d["description"] or rush_label(revision)
        line.quantity, line.unit = Decimal("1"), "each"
        others = [x for x in revision.included_lines() if x.pk != line.pk]
        line.unit_price = rush_amount(revision, others)
        line.unit_cost = ZERO
        line.discount_pct = d["discount_pct"] or ZERO
        return line


LINE_FORMS = {
    QuoteLine.Kind.PRODUCT: ProductLineForm,
    QuoteLine.Kind.ADHOC: AdhocLineForm,
    QuoteLine.Kind.LABOR: LaborLineForm,
    QuoteLine.Kind.MACHINE: MachineLineForm,
    QuoteLine.Kind.MATERIAL: MaterialLineForm,
    QuoteLine.Kind.RUSH: RushLineForm,
}


class OfferForm(forms.Form):
    """How a line is offered (FR-QUO-11)."""

    offer = forms.ChoiceField(
        label="Offered as", choices=QuoteLine.Offer.choices, widget=forms.RadioSelect
    )
    group = forms.CharField(
        max_length=80,
        required=False,
        widget=forms.TextInput(attrs={"list": "offer-groups", "autocomplete": "off"}),
        help_text="What the alternatives are a choice of, such as Wood or Finish. Lines with "
        "the same group are one choice; the customer picks one of them.",
    )
    default = forms.BooleanField(
        label="The default choice",
        required=False,
        help_text="Counted in the total until the customer picks another. Otherwise the "
        "group's first line is.",
    )

    def clean(self):
        data = super().clean()
        if data.get("offer") == QuoteLine.Offer.ALTERNATE and not (data.get("group") or "").strip():
            self.add_error("group", "Name the group, such as Wood.")
        return data


class ApprovalForm(forms.Form):
    approved_by_name = forms.CharField(label="Approved by", max_length=150)
    approved_at = forms.DateTimeField(
        label="When", widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    how = forms.ChoiceField(choices=Approval.How.choices)
    accept_agreement = forms.BooleanField(
        required=False,
        label="The customer also accepted the terms and conditions",
        help_text="Approval covers the quote and the customer agreement.",
    )
    notes = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)


class QuoteTemplateForm(forms.ModelForm):
    class Meta:
        model = QuoteTemplate
        fields = ["name", "is_default", "body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 24, "class": "code"})}


class QuoteSettingsForm(forms.ModelForm):
    class Meta:
        model = QuoteSettings
        fields = ["validity_days"]


# ---------------------------------------------------------------------------
# Starting a quote from the quotes page
# ---------------------------------------------------------------------------


class QuoteForJobForm(forms.Form):
    """A quote for a job that already exists."""

    job = forms.ModelChoiceField(queryset=None, empty_label="Choose a job")
    title = forms.CharField(
        label="Quote title",
        max_length=200,
        required=False,
        help_text="Leave blank to use the job's title.",
    )

    def __init__(self, *args, jobs, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["job"].queryset = jobs
        self.fields["job"].label_from_instance = lambda j: (
            f"{j.number} {j.title} — {j.customer.name}"
        )


class QuoteWithNewJobForm(forms.Form):
    """A new job and its first quote, in one step."""

    title = forms.CharField(label="Job title", max_length=200)
    customer = forms.ModelChoiceField(
        queryset=None,
        required=False,
        empty_label="Walk-in / not known yet",
        help_text="A walk-in placeholder can be replaced with the real customer later.",
    )
    due_date = forms.DateField(label="Due", required=False, widget=DATE)

    def __init__(self, *args, **kwargs):
        from trammel.customers.models import Customer

        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = Customer.objects.filter(
            is_placeholder=False, is_internal=False
        ).order_by("name")
