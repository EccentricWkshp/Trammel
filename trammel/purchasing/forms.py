from __future__ import annotations

from django import forms
from django.db.models import Q

from trammel.core import units
from trammel.shop.models import Material, Vendor
from trammel.workorders.models import ACTIVE, WorkOrder

from .models import POLine, PurchaseOrder


def _decimal_inputs(form):
    for field in form.fields.values():
        if isinstance(field, forms.DecimalField):
            field.widget.attrs["inputmode"] = "decimal"


class OrderForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = [
            "vendor",
            "expected_on",
            "vendor_reference",
            "tax_exempt",
            "exemption_number",
            "tax",
            "shipping",
            "discount",
            "notes",
        ]
        widgets = {
            "expected_on": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "tax": "Spread over the taxable lines by value. Shipping, less the discount, "
            "is spread over all of them.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = self.instance.vendor_id if self.instance.pk else None
        self.fields["vendor"].queryset = Vendor.objects.filter(
            Q(is_active=True) | Q(pk=current)
        ).order_by("name")
        if self.instance.pk and not self.instance._state.adding:
            # The vendor is fixed once lines are priced from them.
            self.fields["vendor"].disabled = True
            if not self.instance.editable:
                for name in ("tax", "shipping", "discount"):
                    self.fields[name].disabled = True
                    self.fields[name].help_text = "Fixed once a delivery has arrived."
        _decimal_inputs(self)


class LineForm(forms.ModelForm):
    class Meta:
        model = POLine
        fields = [
            "material",
            "description",
            "vendor_sku",
            "quantity",
            "unit",
            "unit_price",
            "work_order",
            "tax_exempt",
            "update_cost",
        ]
        labels = {"work_order": "Bought for work order"}
        help_texts = {
            "description": "Filled in from the material; or describe something else.",
            "work_order": "It goes onto this work order when it arrives.",
        }

    def __init__(self, *args, vendor, **kwargs):
        super().__init__(*args, **kwargs)
        self.vendor = vendor
        current = self.instance.material_id
        self.fields["material"].queryset = Material.objects.filter(
            Q(is_active=True) | Q(pk=current)
        ).order_by("name")
        self.fields["material"].required = False
        self.fields["description"].required = False
        self.fields["work_order"].queryset = WorkOrder.objects.filter(
            Q(status__in=ACTIVE) | Q(pk=self.instance.work_order_id)
        ).order_by("-created_at")
        self.fields["work_order"].required = False
        self.fields["unit"].choices = [g for g in units.UNIT_CHOICES if g[0] != "Time"]
        self.fields["unit_price"].required = False
        _decimal_inputs(self)

    def clean(self):
        data = super().clean()
        if not data.get("material") and not (data.get("description") or "").strip():
            self.add_error("material", "Pick a material, or describe the item.")
        if data.get("unit_price") is None:
            if data.get("material"):
                from .services import suggest

                # Blank: the last price paid this vendor, else the material's cost.
                data["unit_price"] = suggest(self.vendor, data["material"])["unit_price"]
            else:
                self.add_error("unit_price", "What does one unit cost?")
        return data
