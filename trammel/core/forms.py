from __future__ import annotations

import zoneinfo

from django import forms
from django.contrib.auth import password_validation

from .models import BusinessProfile, DocumentSequence, ShopRates, SystemSettings

COMMON_CURRENCIES = [
    "USD",
    "CAD",
    "EUR",
    "GBP",
    "AUD",
    "NZD",
    "MXN",
    "JPY",
    "CHF",
    "SEK",
    "NOK",
    "DKK",
]


def timezone_choices():
    return [(z, z) for z in sorted(zoneinfo.available_timezones()) if "/" in z or z == "UTC"]


def currency_choices():
    from babel.numbers import get_currency_name

    return [(c, f"{c} — {get_currency_name(c, locale='en')}") for c in COMMON_CURRENCIES]


class SetupForm(forms.Form):
    """The one-time setup page (FR-ADM-1)."""

    setup_token = forms.CharField(
        required=False,
        widget=forms.PasswordInput,
        help_text="The SETUP_TOKEN value from this instance's environment.",
    )
    business_name = forms.CharField(max_length=200)
    full_name = forms.CharField(label="Your name", max_length=150)
    email = forms.EmailField(label="Your email address")
    password = forms.CharField(widget=forms.PasswordInput, strip=False)
    password_confirm = forms.CharField(
        label="Confirm password", widget=forms.PasswordInput, strip=False
    )
    timezone = forms.ChoiceField(choices=timezone_choices, initial="America/Los_Angeles")
    unit_system = forms.ChoiceField(
        choices=BusinessProfile.UnitSystem.choices, initial=BusinessProfile.UnitSystem.IMPERIAL
    )
    currency = forms.ChoiceField(
        choices=currency_choices,
        initial="USD",
        help_text="Every amount in Trammel is in this currency. It cannot be changed once "
        "a quote or invoice exists.",
    )

    def __init__(self, *args, token_required: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.token_required = token_required
        if not token_required:
            del self.fields["setup_token"]
        else:
            self.fields["setup_token"].required = True

    def clean(self):
        data = super().clean()
        if data.get("password") and data.get("password") != data.get("password_confirm"):
            self.add_error("password_confirm", "The passwords do not match.")
        elif data.get("password"):
            try:
                password_validation.validate_password(data["password"])
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return data


class BusinessProfileForm(forms.ModelForm):
    timezone = forms.ChoiceField(choices=timezone_choices)

    class Meta:
        model = BusinessProfile
        fields = [
            "name",
            "legal_name",
            "phone",
            "email",
            "website",
            "address_line1",
            "address_line2",
            "city",
            "region",
            "postal_code",
            "country",
            "tax_registration_numbers",
            "default_payment_terms_days",
            "timezone",
            "unit_system",
            "week_start",
            "date_format",
            "currency",
        ]
        widgets = {"tax_registration_numbers": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, currency_locked: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["currency"].widget = forms.Select(choices=currency_choices())
        if currency_locked:
            self.fields["currency"].disabled = True
            self.fields[
                "currency"
            ].help_text = "Locked: quotes or invoices already exist in this currency."


class ShopRatesForm(forms.ModelForm):
    class Meta:
        model = ShopRates
        fields = [
            "labor_hourly_rate",
            "labor_daily_rate",
            "shop_overhead_rate",
            "default_material_markup",
            "consumables_allowance",
            "minimum_job_charge",
            "electricity_rate",
        ]


class SystemSettingsForm(forms.ModelForm):
    class Meta:
        model = SystemSettings
        fields = [
            "offline_mode",
            "update_check_enabled",
            "require_mfa_for_admins",
            "audit_retention_days",
        ]


class DocumentSequenceForm(forms.ModelForm):
    class Meta:
        model = DocumentSequence
        fields = ["prefix", "yearly", "padding"]

    def clean_prefix(self):
        prefix = self.cleaned_data["prefix"].strip().upper()
        if not prefix.replace("-", "").isalnum():
            raise forms.ValidationError("Use letters and digits only.")
        return prefix

    def clean_padding(self):
        padding = self.cleaned_data["padding"]
        if not 1 <= padding <= 8:
            raise forms.ValidationError("Between 1 and 8 digits.")
        return padding


class ReasonForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False)
