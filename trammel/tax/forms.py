from django import forms

from .models import Jurisdiction, TaxCategory, TaxRate, TaxSettings

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class TaxSettingsForm(forms.ModelForm):
    class Meta:
        model = TaxSettings
        fields = ["sourcing", "shipping_tax", "manual_enabled", "csv_enabled", "sst_enabled"]


class JurisdictionForm(forms.ModelForm):
    class Meta:
        model = Jurisdiction
        fields = ["code", "name", "level", "region"]


class TaxRateForm(forms.ModelForm):
    class Meta:
        model = TaxRate
        fields = [
            "jurisdiction",
            "kind",
            "name",
            "classification",
            "rate",
            "effective_from",
            "effective_to",
        ]
        widgets = {"effective_from": DATE, "effective_to": DATE}


class BoundaryForm(forms.Form):
    postal_codes = forms.CharField(
        help_text="A ZIP, ZIP+4, or range such as “98402-98409”. One per line.",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    jurisdiction = forms.ModelChoiceField(queryset=Jurisdiction.objects.all())


class TaxCategoryForm(forms.ModelForm):
    class Meta:
        model = TaxCategory
        fields = ["name", "taxable", "gross_receipts_classification", "is_default"]


class ImportForm(forms.Form):
    KINDS = [
        ("sst", "SST rate or boundary file (.zip or .csv)"),
        ("csv", "Generic CSV (postal code → jurisdiction and rate)"),
    ]
    kind = forms.ChoiceField(choices=KINDS)
    url = forms.URLField(
        label="Address of the file",
        required=False,
        assume_scheme="https",
        help_text="For SST files: the published link, e.g. "
        "https://dor.wa.gov/sites/default/files/2026-08/WAB2026Q4.zip "
        "(boundaries) — then the same with WAR (rates).",
    )
    file = forms.FileField(required=False, help_text="…or upload the file.")
    add_to_earlier = forms.BooleanField(
        label="Add to the earlier CSV imports",
        required=False,
        help_text="Generic CSV only. Left unticked, the new file replaces the rates and "
        "postal codes of earlier CSV imports.",
    )

    def clean(self):
        data = super().clean()
        if data.get("kind") == "csv" and not data.get("file"):
            self.add_error("file", "Upload the CSV file.")
        if data.get("kind") == "sst" and not (data.get("file") or data.get("url")):
            self.add_error("url", "Give the file's address or upload it.")
        if data.get("file") and data.get("url"):
            self.add_error("url", "Give an address or a file, not both.")
        return data


class LookupForm(forms.Form):
    postal_code = forms.CharField(max_length=10)
    on = forms.DateField(label="On date", required=False, widget=DATE)
