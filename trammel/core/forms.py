from __future__ import annotations

import zoneinfo

from django import forms
from django.contrib.auth import password_validation

from . import workhours
from .models import (
    WEEKDAYS,
    BusinessProfile,
    DocumentSequence,
    ShopRates,
    SystemSettings,
)

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
    logo_file = forms.FileField(
        label="Upload a logo",
        required=False,
        help_text="PNG, JPEG, WebP, GIF or SVG. Large images are scaled down.",
        widget=forms.FileInput(
            attrs={"accept": "image/png,image/jpeg,image/webp,image/gif,image/svg+xml"}
        ),
    )
    remove_logo = forms.BooleanField(label="Remove the logo", required=False)

    DETAILS = (
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
    )
    REGIONAL = ("timezone", "unit_system", "week_start", "date_format", "currency")
    RESALE = ("resale_certificate_number", "resale_certificate_expires")
    #: A works box, start and end for each day, Monday (0) first. The tab shows
    #: them as a table (hour_rows), and they're kept in shop_hours.
    HOURS = tuple(
        f"day{d}_{part}" for d in range(7) for part in ("works", "starts", "ends", "crew")
    )
    LOGO = ("logo_file", "remove_logo")

    #: The settings page's tabs: (key, label, its titled groups of fields). Each tab
    #: is a form of its own, so saving one leaves the others as they are.
    TABS = (
        ("details", "Details", (("Details", DETAILS),)),
        (
            "documents",
            "Logo and printing",
            (
                ("Logo", LOGO),
                (
                    "Shown on quotes, invoices, agreements and emails",
                    BusinessProfile.DOCUMENT_DETAILS,
                ),
            ),
        ),
        ("region", "Region and format", (("Region and format", REGIONAL),)),
        ("hours", "Shop hours", (("Shop hours", HOURS),)),
        ("tax", "Tax certificate", (("Resale certificate", RESALE),)),
    )

    @classmethod
    def tab_keys(cls) -> list[str]:
        return [key for key, _, _ in cls.TABS]

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
            "resale_certificate_number",
            "resale_certificate_expires",
            *BusinessProfile.DOCUMENT_DETAILS,
            "timezone",
            "unit_system",
            "week_start",
            "date_format",
            "currency",
        ]
        widgets = {
            "tax_registration_numbers": forms.Textarea(attrs={"rows": 3}),
            "resale_certificate_expires": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, tab: str = "details", currency_locked: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._logo = None
        self.tab = tab
        self.groups = next(groups for key, _, groups in self.TABS if key == tab)
        keep = {name for _, names in self.groups for name in names}
        for name in list(self.fields):
            if name not in keep:
                del self.fields[name]
        if "currency" in self.fields:
            self.fields["currency"].widget = forms.Select(choices=currency_choices())
            if currency_locked:
                self.fields["currency"].disabled = True
                self.fields[
                    "currency"
                ].help_text = "Locked: quotes or invoices already exist in this currency."
        if "remove_logo" in self.fields and not self.instance.logo:
            del self.fields["remove_logo"]
        self._shifts = None
        if tab == "hours":
            self._add_hour_fields()

    def _add_hour_fields(self):
        shifts = workhours.shifts_from(self.instance.shop_hours)
        crew = workhours.crew_from(self.instance.shop_crew)

        def clock(label):
            return forms.TimeInput(attrs={"type": "time", "aria-label": label}, format="%H:%M")

        for d, name in WEEKDAYS:
            shift = shifts[d]
            self.fields[f"day{d}_works"] = forms.BooleanField(
                label=name, required=False, initial=shift is not None
            )
            for i, part in enumerate(("starts", "ends")):
                self.fields[f"day{d}_{part}"] = forms.TimeField(
                    label=f"{name} {part}",
                    required=False,
                    initial=shift[i] if shift else None,
                    widget=clock(f"{name} {part}"),
                )
            self.fields[f"day{d}_crew"] = forms.IntegerField(
                label=f"People working {name}",
                required=False,
                min_value=0,
                max_value=99,
                initial=crew[d],
                widget=forms.NumberInput(
                    attrs={"aria-label": f"People working {name}", "class": "crew-input"}
                ),
            )

    def hour_rows(self):
        """The hours tab's table: a row per day, from the day the week starts on,
        with the saved shift's length."""
        start = self.instance.week_start
        shifts = workhours.shifts_from(self.instance.shop_hours)
        rows = []
        for d, name in sorted(WEEKDAYS, key=lambda day: (day[0] - start) % 7):
            shift = shifts[d]
            rows.append(
                {
                    "name": name,
                    "works": self[f"day{d}_works"],
                    "starts": self[f"day{d}_starts"],
                    "ends": self[f"day{d}_ends"],
                    "crew": self[f"day{d}_crew"],
                    "minutes": workhours.shift_minutes(*shift) if shift else 0,
                    "overnight": bool(shift) and shift[1] <= shift[0],
                }
            )
        return rows

    def clean(self):
        data = super().clean()
        if self.tab == "hours":
            self._clean_hours(data)
        return data

    def _clean_hours(self, data):
        names = dict(WEEKDAYS)
        shifts = []
        for d in range(7):
            if not data.get(f"day{d}_works"):
                shifts.append(None)
                continue
            missing = [p for p in ("starts", "ends") if data.get(f"day{d}_{p}") is None]
            for part in missing:
                if f"day{d}_{part}" not in self.errors:
                    self.add_error(f"day{d}_{part}", f"Give the time {names[d]} {part}.")
            shifts.append(None if missing else (data[f"day{d}_starts"], data[f"day{d}_ends"]))
        self._crew = [
            1 if data.get(f"day{d}_crew") is None else data[f"day{d}_crew"] for d in range(7)
        ]
        if self.errors:
            return
        if not any(shifts):
            self.add_error(None, "Pick at least one working day.")
            return
        for d, shift in enumerate(shifts):
            after = (d + 1) % 7
            following = shifts[after]
            if not (shift and following):
                continue
            runs_to = shift[0].hour * 60 + shift[0].minute + workhours.shift_minutes(*shift)
            if workhours.DAY + following[0].hour * 60 + following[0].minute < runs_to:
                self.add_error(
                    f"day{after}_starts",
                    f"{names[after]} can't start before {names[d]}'s hours end.",
                )
        self._shifts = shifts

    def sections(self):
        """This tab's fields in titled groups."""
        return [
            (title, [self[n] for n in names if n in self.fields]) for title, names in self.groups
        ]

    def clean_logo_file(self):
        from .logo import prepare

        upload = self.cleaned_data.get("logo_file")
        if upload:
            self._logo = prepare(upload)
        return upload

    def save(self, commit=True):
        profile = super().save(commit=False)
        if self._shifts is not None:
            profile.shop_hours = workhours.stored(self._shifts)
            profile.shop_crew = self._crew
        if self._logo:
            profile.logo, profile.logo_type = self._logo
        elif self.cleaned_data.get("remove_logo"):
            profile.logo, profile.logo_type = b"", ""
        if commit:
            profile.save()
        return profile


class ShopRatesForm(forms.ModelForm):
    class Meta:
        model = ShopRates
        fields = [
            "labor_hourly_rate",
            "labor_daily_rate",
            "labor_cost_rate",
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
