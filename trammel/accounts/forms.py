from __future__ import annotations

from django import forms

from trammel.core import modules
from trammel.core.forms import timezone_choices

from .models import Role, User


class LoginForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autocomplete": "username", "autofocus": True})
    )
    password = forms.CharField(
        strip=False, widget=forms.PasswordInput(attrs={"autocomplete": "current-password"})
    )


def _blank_timezone_choices():
    return [("", "Business default"), *timezone_choices()]


class UserForm(forms.ModelForm):
    timezone = forms.ChoiceField(choices=_blank_timezone_choices, required=False)

    class Meta:
        model = User
        fields = ["full_name", "email", "is_admin", "roles", "timezone", "labor_cost_rate"]
        widgets = {"roles": forms.CheckboxSelectMultiple}
        help_texts = {
            "is_admin": "Full access, including users, roles, modules and system settings.",
        }

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another user already has this email address.")
        return email


class ProfileForm(forms.ModelForm):
    timezone = forms.ChoiceField(choices=_blank_timezone_choices, required=False)

    class Meta:
        model = User
        fields = ["full_name", "timezone"]


def permission_choices():
    """Permissions grouped by module, for the role editor."""
    return [
        (m.name, [(p.codename, p.label) for p in m.permissions])
        for m in modules.manifests()
        if m.permissions
    ]


class RoleForm(forms.ModelForm):
    permissions = forms.MultipleChoiceField(
        choices=permission_choices, widget=forms.CheckboxSelectMultiple, required=False
    )

    class Meta:
        model = Role
        fields = ["name", "description", "require_mfa", "permissions"]


class ApiKeyForm(forms.Form):
    name = forms.CharField(max_length=80, help_text="What will use this key, e.g. “Label printer”.")
    scopes = forms.MultipleChoiceField(
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="A key can never do more than you can.",
    )
    expires_in_days = forms.IntegerField(
        min_value=1, max_value=3650, required=False, help_text="Leave blank for no expiry."
    )

    def __init__(self, *args, held: frozenset[str], **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scopes"].choices = [
            (group, [(c, label) for c, label in perms if c in held])
            for group, perms in permission_choices()
            if any(c in held for c, _ in perms)
        ]
