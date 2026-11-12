"""The edge portal's settings page (admins): the Cloudflare token and address,
deploying, syncing now, and stopping."""

from __future__ import annotations

import re

from django import forms
from django.contrib import messages
from django.db.models import Count
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from trammel.core import secrets, tasks
from trammel.core.web import admin_required

from . import client, cloudflare, deploy
from .models import EdgeSettings, Published

HOSTNAME = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class SecretBox(forms.TextInput):
    """Write-only: never shown again, and not mistaken for a sign-in form."""

    def format_value(self, value):
        return None


class EdgeForm(forms.ModelForm):
    api_token = forms.CharField(
        label="Cloudflare API token",
        required=False,
        widget=SecretBox(
            attrs={
                "autocomplete": "off",
                "spellcheck": "false",
                "class": "secret-box",
                "data-1p-ignore": "",
                "data-lpignore": "true",
            }
        ),
        help_text="Made in Cloudflare's dashboard (My Profile, API Tokens, Create Token, "
        "Custom token) with the permissions listed on this page.",
    )

    class Meta:
        model = EdgeSettings
        fields = ["hostname", "account_id"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if secrets.is_set(deploy.TOKEN_KEY):
            self.fields["api_token"].help_text += " Saved; leave blank to keep it."

    def clean_hostname(self):
        value = self.cleaned_data["hostname"].strip().lower().rstrip(".")
        if value.startswith(("http://", "https://")):
            value = value.split("://", 1)[1].split("/", 1)[0]
        if value and not HOSTNAME.match(value):
            raise forms.ValidationError("Enter a host name, such as portal.example.com.")
        return value

    def save(self, commit=True):
        row = super().save(commit=commit)
        token = self.cleaned_data.get("api_token", "").strip()
        if token:
            secrets.put(deploy.TOKEN_KEY, token)
        return row


@admin_required
def settings_page(request):
    row = EdgeSettings.load()
    form = EdgeForm(request.POST or None, instance=row)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Saved. Deploy to put the changes on Cloudflare.")
        return redirect("edge:settings")
    counts = {p["type"]: p["n"] for p in Published.objects.values("type").annotate(n=Count("id"))}
    return render(
        request,
        "edge/settings.html",
        {
            "form": form,
            "row": row,
            "token_set": secrets.is_set(deploy.TOKEN_KEY),
            "permissions": cloudflare.PERMISSIONS,
            "counts": counts,
            **_ebay(row),
        },
    )


def _ebay(row) -> dict:
    """Where eBay sends account deletion notices, when the portal takes them."""
    from trammel.core import modules

    if not (row.active and modules.is_enabled("imports")):
        return {}
    from trammel.imports.models import EbaySettings

    if not EbaySettings.load().enabled:
        return {}
    return {
        "ebay_endpoint": f"{row.public_url}/imports/ebay/deletion/",
        "ebay_token": deploy.ebay_token(),
    }


@admin_required
@require_POST
def start_deploy(request):
    row = EdgeSettings.load()
    if row.deploying:
        messages.info(request, "Already deploying.")
    else:
        EdgeSettings.objects.filter(pk=1).update(deploying=True, deploy_log=[])
        tasks.enqueue("edge.deploy", dedupe=True)
        messages.success(
            request, "Deploying to Cloudflare. This page follows along; it takes a minute or two."
        )
    return redirect("edge:settings")


@admin_required
@require_POST
def sync_now(request):
    from . import sync

    result = sync.run()
    if not EdgeSettings.load().active:
        messages.error(request, "Deploy the edge portal first.")
    elif result.get("error"):
        messages.error(request, f"The sync failed: {result['error']}")
    else:
        messages.success(
            request,
            f"Synced: {result.get('pushed', 0)} published, {result.get('removed', 0)} taken down, "
            f"{result.get('applied', 0)} customer actions applied.",
        )
    return redirect("edge:settings")


@admin_required
@require_POST
def stop(request):
    try:
        deploy.stop()
    except client.EdgeError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            "Stopped. Everything published to Cloudflare is removed and customers are signed "
            "out there; Trammel's own portal pages are back.",
        )
    return redirect("edge:settings")
