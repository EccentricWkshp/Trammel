from __future__ import annotations

from django import forms
from django.conf import settings

from .models import PortalSettings


class UploadSettingsForm(forms.ModelForm):
    class Meta:
        model = PortalSettings
        fields = ["uploads_enabled", "upload_limit_mb"]

    def clean_upload_limit_mb(self):
        value = self.cleaned_data["upload_limit_mb"]
        if not value:
            raise forms.ValidationError("Allow at least 1 MB.")
        if value > settings.MAX_UPLOAD_MB:
            raise forms.ValidationError(
                f"Files staff upload are limited to {settings.MAX_UPLOAD_MB} MB; customers' "
                "can be no larger."
            )
        return value
