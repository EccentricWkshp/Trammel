from django import forms
from django.db.models import Q

from trammel.core import units

from .models import Machine, Material, MaterialBarcode, MaterialUnit, Vendor

WEAR_ROWS = 4


class MachineForm(forms.ModelForm):
    """A machine, with the rate calculator's inputs grouped for the template."""

    GROUPS = (
        ("Purchase and life", ["purchase_cost", "salvage_value", "lifespan_hours"]),
        ("Maintenance", ["maintenance_cost", "maintenance_interval_hours"]),
        ("Running costs", ["power_kw", "consumables_per_hour"]),
        ("Fixed costs", ["annual_fixed_costs", "annual_hours"]),
        ("Pricing", ["failure_pct", "markup_pct"]),
    )

    class Meta:
        model = Machine
        fields = [
            "name",
            "kind",
            "group",
            "runs_unattended",
            "hourly_rate",
            "notes",
            "purchase_cost",
            "salvage_value",
            "lifespan_hours",
            "annual_hours",
            "maintenance_cost",
            "maintenance_interval_hours",
            "power_kw",
            "consumables_per_hour",
            "annual_fixed_costs",
            "failure_pct",
            "markup_pct",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["hourly_rate"].label = "Hourly rate charged"
        for name in self.Meta.fields:
            if name not in ("name", "kind", "group", "runs_unattended", "notes"):
                self.fields[name].widget.attrs.update({"inputmode": "decimal", "data-rate": name})
        self.fields["kind"].widget.attrs["data-rate"] = "kind"
        self.fields["group"].widget.attrs.update({"list": "machine-groups", "autocomplete": "off"})
        parts = list(self.instance.wear_parts or [])
        for i in range(WEAR_ROWS):
            part = parts[i] if i < len(parts) else {}
            self.fields[f"wear_{i}_name"] = forms.CharField(
                required=False, initial=part.get("name", ""), label="Part"
            )
            self.fields[f"wear_{i}_cost"] = forms.DecimalField(
                required=False,
                min_value=0,
                max_digits=12,
                decimal_places=2,
                initial=part.get("cost"),
                label="Cost",
            )
            self.fields[f"wear_{i}_life"] = forms.IntegerField(
                required=False, min_value=1, initial=part.get("life_hours"), label="Lasts (hours)"
            )
            for key, what in (("name", "name"), ("cost", "cost"), ("life", "life in hours")):
                self.fields[f"wear_{i}_{key}"].widget.attrs.update(
                    {"data-wear": f"{i}-{key}", "aria-label": f"Wear part {i + 1} {what}"}
                )

    def group_fields(self):
        return [(title, [self[name] for name in names]) for title, names in self.GROUPS]

    def wear_rows(self):
        return [
            (self[f"wear_{i}_name"], self[f"wear_{i}_cost"], self[f"wear_{i}_life"])
            for i in range(WEAR_ROWS)
        ]

    def clean(self):
        data = super().clean()
        parts = []
        for i in range(WEAR_ROWS):
            name, cost, life = (data.get(f"wear_{i}_{k}") for k in ("name", "cost", "life"))
            if name or cost or life:
                if not (cost and life):
                    self.add_error(f"wear_{i}_cost", "Give the part's cost and how long it lasts.")
                    continue
                parts.append(
                    {"name": (name or "Wear part").strip(), "cost": str(cost), "life_hours": life}
                )
        self.cleaned_wear_parts = parts
        return data

    def save(self, commit=True):
        self.instance.wear_parts = getattr(self, "cleaned_wear_parts", self.instance.wear_parts)
        return super().save(commit=commit)


class MaterialForm(forms.ModelForm):
    class Meta:
        model = Material
        fields = [
            "name",
            "category",
            "thickness",
            "vendor",
            "sku",
            "url",
            "unit",
            "unit_cost",
            "markup",
            "sheet_width",
            "sheet_length",
            "sheet_dimension_unit",
            "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = self.instance.vendor_id
        self.fields["vendor"].queryset = Vendor.objects.filter(
            Q(is_active=True) | Q(pk=current)
        ).order_by("name")
        self.fields["vendor"].empty_label = "—"


class _ForMaterial(forms.ModelForm):
    def __init__(self, *args, material, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.material = material
        for field in self.fields.values():
            if isinstance(field, forms.DecimalField):
                field.widget.attrs["inputmode"] = "decimal"


class MaterialUnitForm(_ForMaterial):
    """“[100] [each] = [1] box”: the right-hand unit is the material's stock unit."""

    class Meta:
        model = MaterialUnit
        fields = ["quantity", "unit", "stock_quantity"]
        labels = {"quantity": "Amount", "stock_quantity": "Equals"}

    def __init__(self, *args, material, **kwargs):
        super().__init__(*args, material=material, **kwargs)
        self.fields["unit"].choices = [
            ("", "—"),
            *[
                (dim, [(c, label) for c, label in group if c != material.unit])
                for dim, group in units.UNIT_CHOICES
                if dim != "Time"
            ],
        ]
        self.fields["stock_quantity"].help_text = units.get(material.unit).label


class MaterialBarcodeForm(_ForMaterial):
    class Meta:
        model = MaterialBarcode
        fields = ["code", "quantity", "unit"]
        labels = {"code": "Barcode", "quantity": "One scan is"}

    def __init__(self, *args, material, **kwargs):
        super().__init__(*args, material=material, **kwargs)
        self.fields["unit"].choices = [("", "—"), *material.unit_choices()]
        self.fields["unit"].required = False
        self.fields["code"].widget.attrs["autocomplete"] = "off"

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        if not code:
            raise forms.ValidationError("Enter the barcode.")
        clash = MaterialBarcode.objects.filter(code=code).select_related("material").first()
        if clash is not None:
            raise forms.ValidationError(f"{code} is already the barcode of {clash.material}.")
        return code


class VendorForm(forms.ModelForm):
    class Meta:
        model = Vendor
        fields = [
            "name",
            "contact_name",
            "email",
            "phone",
            "website",
            "account_number",
            "payment_terms",
            "lead_time_days",
            "address",
            "notes",
        ]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }
