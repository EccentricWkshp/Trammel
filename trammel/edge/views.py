"""The edge portal's settings page (admins): the Cloudflare token and address,
deploying, syncing now, and stopping."""

from __future__ import annotations

import re

from django import forms
from django.contrib import messages
from django.db.models import Count
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from trammel.core import audit, secrets
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
        help_text="Made in Cloudflare's dashboard with the permissions listed on this page: "
        "an account API token (Manage Account, Account API Tokens), which Cloudflare "
        "recommends, or a user token (My Profile, API Tokens).",
    )
    turnstile_secret = forms.CharField(
        label="Turnstile secret key",
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
        help_text="The secret key of the Turnstile widget above.",
    )

    class Meta:
        model = EdgeSettings
        fields = [
            "account_id",
            "hostname",
            "keep_closed_days",
            "turnstile_own_sitekey",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if secrets.is_set(deploy.TOKEN_KEY):
            self.fields["api_token"].help_text += " Saved; leave blank to keep it."
        if secrets.is_set(deploy.TURNSTILE_OWN_KEY):
            self.fields["turnstile_secret"].help_text += " Saved; leave blank to keep it."

    def clean_account_id(self):
        value = self.cleaned_data["account_id"].strip().lower()
        if value and not re.fullmatch(r"[0-9a-f]{32}", value):
            raise forms.ValidationError(
                "A Cloudflare account ID is 32 letters and digits, such as "
                "0123456789abcdef0123456789abcdef."
            )
        return value

    def clean(self):
        data = super().clean()
        sitekey = (data.get("turnstile_own_sitekey") or "").strip()
        data["turnstile_own_sitekey"] = sitekey
        if (
            sitekey
            and not (data.get("turnstile_secret") or "").strip()
            and not secrets.is_set(deploy.TURNSTILE_OWN_KEY)
        ):
            self.add_error("turnstile_secret", "Paste the widget's secret key too.")
        return data

    def clean_hostname(self):
        value = self.cleaned_data["hostname"].strip().lower().rstrip(".")
        if value.startswith(("http://", "https://")):
            value = value.split("://", 1)[1].split("/", 1)[0]
        if value and not HOSTNAME.match(value):
            raise forms.ValidationError("Enter a host name, such as portal.example.com.")
        return value

    def save(self, commit=True):
        before = audit.snapshot(self.instance, fields=self.Meta.fields)
        row = super().save(commit=commit)
        token = self.cleaned_data.get("api_token", "").strip()
        changed = [f.replace("_", " ") for f in self.changed_data if f in self.Meta.fields]
        if token:
            secrets.put(deploy.TOKEN_KEY, token)
            changed.append("API token")
        if not row.turnstile_own_sitekey:
            secrets.clear(deploy.TURNSTILE_OWN_KEY)
        elif turnstile := self.cleaned_data.get("turnstile_secret", "").strip():
            secrets.put(deploy.TURNSTILE_OWN_KEY, turnstile)
            changed.append("Turnstile secret key")
        if changed:
            old, new = audit.diff(before, audit.snapshot(row, fields=self.Meta.fields))
            audit.record(
                "update",
                row,
                summary=f"Changed the portal on Cloudflare's settings: {', '.join(changed)}",
                before=old,
                after=new,
            )
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
    if not deploy.start():
        messages.info(request, "Already deploying.")
    else:
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
def remove(request):
    try:
        deploy.remove()
    except (deploy.DeployError, cloudflare.CloudflareError, client.EdgeError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            "Removed from Cloudflare: the Worker, its database, its storage and its bot check. "
            "Deploying again starts afresh.",
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
