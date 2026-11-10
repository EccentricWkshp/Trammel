from __future__ import annotations

from django import forms
from django.db.models import Q
from django.utils import timezone

from trammel.shop.models import Vendor

from .models import Location, StockItem, StockSettings

DEC = {"max_digits": 14, "decimal_places": 4}


def _decimal(**kw) -> forms.DecimalField:
    field = forms.DecimalField(**DEC, **kw)
    field.widget.attrs["inputmode"] = "decimal"
    return field


def _locations(current=None):
    return Location.objects.filter(Q(is_active=True) | Q(pk=getattr(current, "pk", None)))


class _UnitMixin:
    def _unit_field(self, material, *, label="Unit"):
        self.fields["unit"] = forms.ChoiceField(
            label=label, choices=material.unit_choices(), initial=material.unit
        )


class LocationForm(forms.ModelForm):
    class Meta:
        model = Location
        fields = ["name", "parent", "is_active", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"parent": "Inside"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = _locations(self.instance.parent)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = qs
        self.fields["parent"].empty_label = "— nothing (a top-level location) —"


class StockSectionForm(forms.Form):
    """The stock part of adding a material, or of starting to track one: where it
    is kept, when to restock it, and what is on hand now."""

    keep = forms.BooleanField(
        label="Keep it in stock",
        required=False,
        initial=True,
        help_text="Untick for things bought for one job. You can start tracking it later.",
    )
    location = forms.ModelChoiceField(
        Location.objects.none(), required=False, empty_label="—", label="Kept in"
    )
    new_location = forms.CharField(
        label="or a new location", max_length=100, required=False, help_text="e.g. “Shelf 3”"
    )
    min_quantity = _decimal(label="Restock at", required=False, min_value=0)
    reorder_quantity = _decimal(label="Reorder", required=False, min_value=0)
    on_hand = _decimal(
        label="On hand now",
        required=False,
        min_value=0,
        help_text="In the unit it is counted in. Leave blank to add stock later.",
    )
    on_hand_cost = forms.DecimalField(
        label="Its cost per unit",
        required=False,
        min_value=0,
        max_digits=14,
        decimal_places=4,
        help_text="Leave blank to use the material's cost.",
    )

    def __init__(self, *args, can_receive: bool, optional: bool, **kwargs):
        kwargs.setdefault("prefix", "stock")
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = _locations()
        if not optional:
            del self.fields["keep"]
        if not can_receive:
            del self.fields["on_hand"], self.fields["on_hand_cost"]
        else:
            self.fields["on_hand_cost"].widget.attrs["inputmode"] = "decimal"

    @property
    def wanted(self) -> bool:
        return "keep" not in self.fields or bool(self.cleaned_data.get("keep"))

    def clean(self):
        data = super().clean()
        where = data.get("location") or (data.get("new_location") or "").strip()
        if self.wanted and not where:
            self.add_error("location", "Say where it is kept.")
        return data


class TrackingForm(forms.ModelForm):
    class Meta:
        model = StockItem
        fields = ["tracked", "min_quantity", "reorder_quantity", "default_location"]
        labels = {"tracked": "Track this material in stock"}

    def __init__(self, *args, material, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance._state.adding:
            self.initial.setdefault("tracked", True)
        self.fields["default_location"].queryset = _locations(self.instance.default_location)
        self.fields["default_location"].empty_label = "—"
        unit = material.get_unit_display()
        for name in ("min_quantity", "reorder_quantity"):
            self.fields[name].help_text += f" In {unit}."
            self.fields[name].widget.attrs["inputmode"] = "decimal"


class ReceiveForm(_UnitMixin, forms.Form):
    KIND = (("receive", "Received"), ("opening", "Already on hand (opening balance)"))

    kind = forms.ChoiceField(label="What is this?", choices=KIND, initial="receive")
    quantity = _decimal(min_value=0)
    unit_cost = forms.DecimalField(
        label="Cost per unit", required=False, min_value=0, max_digits=14, decimal_places=4
    )
    total_cost = forms.DecimalField(
        label="or total cost", required=False, min_value=0, max_digits=12, decimal_places=2
    )
    location = forms.ModelChoiceField(Location.objects.none(), empty_label=None)
    received_on = forms.DateField(
        label="Received", initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"})
    )
    vendor = forms.ModelChoiceField(Vendor.objects.none(), required=False, empty_label="—")
    update_cost = forms.BooleanField(
        label="Make this the material's cost (quotes use it)", required=False, initial=True
    )
    note = forms.CharField(max_length=300, required=False)

    def __init__(self, *args, material, item, **kwargs):
        super().__init__(*args, **kwargs)
        self._unit_field(material)
        self.order_fields(["kind", "quantity", "unit"])
        self.fields["location"].queryset = _locations()
        if item is not None and item.default_location_id:
            self.fields["location"].initial = item.default_location_id
        self.fields["vendor"].queryset = Vendor.objects.filter(is_active=True)
        self.fields["vendor"].initial = material.vendor_id
        for name in ("unit_cost", "total_cost"):
            self.fields[name].widget.attrs["inputmode"] = "decimal"

    def clean(self):
        data = super().clean()
        if data.get("unit_cost") is None and data.get("total_cost") is None:
            self.add_error("unit_cost", "Give the cost per unit or the total.")
        return data


class CountForm(_UnitMixin, forms.Form):
    location = forms.ModelChoiceField(Location.objects.none(), empty_label=None)
    counted = _decimal(min_value=0, label="Counted")
    #: What the counter was shown, so a change made meanwhile is not overwritten.
    expected = forms.DecimalField(**DEC, widget=forms.HiddenInput, required=False)
    note = forms.CharField(max_length=300, required=False)

    def __init__(self, *args, material, item, **kwargs):
        super().__init__(*args, **kwargs)
        self._unit_field(material)
        self.order_fields(["location", "counted", "unit"])
        self.fields["location"].queryset = _locations()
        if item is not None and item.default_location_id:
            self.fields["location"].initial = item.default_location_id


class LotActionForm(_UnitMixin, forms.Form):
    """Move or scrap some of a lot."""

    quantity = _decimal(min_value=0)
    to = forms.ModelChoiceField(Location.objects.none(), required=False, label="To")
    note = forms.CharField(max_length=300, required=False)

    def __init__(self, *args, material, **kwargs):
        super().__init__(*args, **kwargs)
        self._unit_field(material)
        self.fields["to"].queryset = _locations()


class SettingsForm(forms.ModelForm):
    class Meta:
        model = StockSettings
        fields = ["shortfall"]
        widgets = {"shortfall": forms.RadioSelect}
