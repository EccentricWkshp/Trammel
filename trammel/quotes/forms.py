from __future__ import annotations

from decimal import Decimal

from django import forms

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
            "internal_notes",
            "labor_rate",
            "overhead_rate",
            "material_markup",
            "consumables_pct",
            "minimum_charge",
        ]
        widgets = {
            "valid_until": DATE,
            "customer_notes": forms.Textarea(attrs={"rows": 3}),
            "internal_notes": forms.Textarea(attrs={"rows": 2}),
        }

    def clean(self):
        data = super().clean()
        if data.get("deposit_kind") == "percent" and (data.get("deposit_value") or 0) > 100:
            self.add_error("deposit_value", "A percentage deposit cannot exceed 100.")
        return data


class _LineForm(forms.Form):
    discount_pct = _pct()

    def build(self, revision: QuoteRevision, line: QuoteLine | None = None) -> QuoteLine:
        raise NotImplementedError

    @staticmethod
    def initial_from(line: QuoteLine) -> dict:
        return {
            "description": line.description,
            "quantity": line.quantity,
            "discount_pct": line.discount_pct,
        }


class ProductLineForm(_LineForm):
    product = forms.ModelChoiceField(Product.objects.filter(is_active=True))
    quantity = _qty()
    description = forms.CharField(required=False, help_text="Leave blank for the product name.")

    def build(self, revision, line=None):
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

    def build(self, revision, line=None):
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

    def build(self, revision, line=None):
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

    def build(self, revision, line=None):
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

    def build(self, revision, line=None):
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


LINE_FORMS = {
    QuoteLine.Kind.PRODUCT: ProductLineForm,
    QuoteLine.Kind.ADHOC: AdhocLineForm,
    QuoteLine.Kind.LABOR: LaborLineForm,
    QuoteLine.Kind.MACHINE: MachineLineForm,
    QuoteLine.Kind.MATERIAL: MaterialLineForm,
}


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
