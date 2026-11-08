from django import forms

from .models import Address, Contact, Customer


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            "kind",
            "name",
            "company",
            "email",
            "phone",
            "payment_terms_days",
            "tax_exempt",
            "tax_exempt_number",
            "tax_exempt_expires",
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
            "tax_exempt_expires": forms.DateInput(attrs={"type": "date"}),
        }


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["name", "role", "email", "phone", "is_primary", "notes"]


class AddressForm(forms.ModelForm):
    class Meta:
        model = Address
        fields = [
            "label",
            "line1",
            "line2",
            "city",
            "region",
            "postal_code",
            "country",
            "is_default_billing",
            "is_default_shipping",
        ]

    def clean_country(self):
        return self.cleaned_data["country"].strip().upper()
