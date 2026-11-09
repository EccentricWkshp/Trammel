from __future__ import annotations

from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from trammel.core import audit, secrets, tasks
from trammel.core.services import save_audited
from trammel.core.web import admin_required

from . import backup, trash, updates
from .models import Backup, BackupSettings, Export


class BackupSettingsForm(forms.ModelForm):
    smb_password = forms.CharField(
        label="Password",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Stored encrypted and never shown again. Leave blank to keep the current one.",
    )
    smb_clear_password = forms.BooleanField(label="Forget the saved password", required=False)
    s3_secret_key = forms.CharField(
        label="Secret access key",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Stored encrypted and never shown again. Leave blank to keep the current one.",
    )
    s3_clear_secret = forms.BooleanField(label="Forget the saved secret key", required=False)

    #: Each off-box target: its switch, the fields shown while it is on, and the
    #: fields it cannot work without.
    TARGETS = (
        (
            "smb_enabled",
            (
                "smb_server",
                "smb_share",
                "smb_folder",
                "smb_username",
                "smb_password",
                "smb_clear_password",
            ),
            ("smb_server", "smb_share"),
        ),
        (
            "s3_enabled",
            (
                "s3_endpoint",
                "s3_region",
                "s3_bucket",
                "s3_prefix",
                "s3_access_key",
                "s3_secret_key",
                "s3_clear_secret",
            ),
            ("s3_endpoint", "s3_access_key"),
        ),
        ("offbox_enabled", ("offbox_path",), ("offbox_path",)),
    )
    GENERAL = ("scheduled", "interval_hours", "keep", "warn_after_days")

    class Meta:
        model = BackupSettings
        fields = [
            "scheduled",
            "interval_hours",
            "keep",
            "warn_after_days",
            "smb_enabled",
            "smb_server",
            "smb_share",
            "smb_folder",
            "smb_username",
            "s3_enabled",
            "s3_endpoint",
            "s3_region",
            "s3_bucket",
            "s3_prefix",
            "s3_access_key",
            "offbox_enabled",
            "offbox_path",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for secret, field, clear, saved in (
            (BackupSettings.SMB_SECRET, "smb_password", "smb_clear_password", "A password"),
            (BackupSettings.S3_SECRET, "s3_secret_key", "s3_clear_secret", "A secret key"),
        ):
            if secrets.is_set(secret):
                self.fields[
                    field
                ].help_text = f"{saved} is saved (encrypted). Leave blank to keep it."
            else:
                del self.fields[clear]
        self.fields[
            "smb_username"
        ].help_text = "Leave blank for a share open to guests. For a domain account, DOMAIN\\name."

    def general(self):
        return [self[n] for n in self.GENERAL]

    def targets(self):
        """(switch, fields shown while it is on) for each off-box target."""
        return [
            (self[switch], [self[n] for n in names if n in self.fields])
            for switch, names, _ in self.TARGETS
        ]

    def clean(self):
        data = super().clean()
        for switch, _, required in self.TARGETS:
            if data.get(switch):
                for name in required:
                    if not str(data.get(name) or "").strip():
                        self.add_error(name, "Needed while this copy is switched on.")
        # S3 cannot sign without a secret key; a share may allow guest access.
        no_s3_secret = data.get("s3_clear_secret") or not secrets.is_set(BackupSettings.S3_SECRET)
        if data.get("s3_enabled") and not data.get("s3_secret_key") and no_s3_secret:
            self.add_error("s3_secret_key", "Needed while this copy is switched on.")
        return data

    def save_secret(self):
        for secret, field, clear in (
            (BackupSettings.SMB_SECRET, "smb_password", "smb_clear_password"),
            (BackupSettings.S3_SECRET, "s3_secret_key", "s3_clear_secret"),
        ):
            if self.cleaned_data.get(clear):
                secrets.clear(secret)
            if value := self.cleaned_data.get(field):
                secrets.put(secret, value)


@admin_required
def maintenance(request):
    obj = BackupSettings.load()
    before = audit.snapshot(obj)
    form = BackupSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        save_audited(form.save(commit=False), before=before, summary="Edited backup settings")
        form.save_secret()
        # "Save and test" buttons: test exactly what is on the screen, once saved.
        test = {"smb": backup.test_smb, "s3": backup.test_s3}.get(request.POST.get("test", ""))
        if test is None:
            messages.success(request, "Backup settings saved.")
        else:
            result = test()
            report = messages.success if result.startswith("OK") else messages.error
            report(request, f"Settings saved. {result}")
        return redirect(reverse("system:maintenance") + "?settings=open")
    return render(
        request,
        "system/maintenance.html",
        {
            "form": form,
            "backups": Backup.objects.all()[:30],
            "exports": Export.objects.all()[:10],
            "last": backup.last_success(),
            "warning": backup.age_warning(),
            "backup_dir": settings.BACKUP_DIR,
        },
    )


@admin_required
@require_POST
def test_s3(request):
    result = backup.test_s3()
    (messages.success if result.startswith("OK") else messages.error)(request, result)
    return redirect("system:maintenance")


@admin_required
@require_POST
def test_smb(request):
    result = backup.test_smb()
    (messages.success if result.startswith("OK") else messages.error)(request, result)
    return redirect("system:maintenance")


@admin_required
@require_POST
def backup_now(request):
    tasks.enqueue("system.backup_now", {"kind": "manual"})
    messages.success(request, "Backup started. It appears below when the worker finishes.")
    return redirect("system:maintenance")


@admin_required
@require_POST
def export_now(request):
    tasks.enqueue("system.export")
    messages.success(request, "Export started. Download it below when it is ready.")
    return redirect("system:maintenance")


@admin_required
@require_POST
def check_updates(request):
    status = updates.check()
    if status.error:
        messages.warning(request, status.error)
    elif updates.available():
        messages.info(request, f"Trammel {status.latest_version} is available.")
    else:
        messages.success(request, "Trammel is up to date.")
    return redirect("core:system")


def _download(path: Path, name: str):
    if not path.exists():
        raise Http404
    return FileResponse(path.open("rb"), as_attachment=True, filename=name)


@admin_required
def download_backup(request, pk):
    record = get_object_or_404(Backup, pk=pk, status=Backup.Status.OK)
    audit.record(
        "backup.download",
        entity_type="system.backup",
        entity_id=str(pk),
        summary=f"Downloaded backup {record.filename}",
    )
    return _download(backup.backup_dir() / record.filename, record.filename)


@admin_required
def download_export(request, pk):
    record = get_object_or_404(Export, pk=pk, status="ok")
    audit.record(
        "export.download",
        entity_type="system.export",
        entity_id=str(pk),
        summary=f"Downloaded export {record.filename}",
    )
    return _download(Path(settings.BACKUP_DIR) / "exports" / record.filename, record.filename)


@admin_required
def restore_confirm(request, pk):
    record = get_object_or_404(Backup, pk=pk, status=Backup.Status.OK)
    path = backup.backup_dir() / record.filename
    try:
        manifest = backup.read_manifest(path)
        newer = backup.check_compatible(manifest)
    except (OSError, backup.RestoreRefused) as exc:
        messages.error(request, str(exc))
        return redirect("system:maintenance")
    if request.method == "POST":
        if request.POST.get("confirm") != "RESTORE":
            messages.error(request, "Type RESTORE to confirm.")
        elif newer:
            messages.error(request, "This backup is from a newer version of Trammel.")
        else:
            try:
                backup.restore(path)
            except backup.RestoreRefused as exc:
                messages.error(request, str(exc))
            except Exception as exc:  # noqa: BLE001 — report pg_restore's own message
                messages.error(request, f"The restore failed: {exc}")
            else:
                messages.success(request, "Restored. Sign in again if asked.")
            return redirect("system:maintenance")
    return render(
        request, "system/restore.html", {"record": record, "manifest": manifest, "newer": newer}
    )


@admin_required
def trash_list(request):
    return render(request, "system/trash.html", {"items": trash.items()})


@admin_required
@require_POST
def trash_restore(request, label, pk):
    try:
        obj = trash.restore(label, pk)
    except LookupError:
        raise Http404 from None
    messages.success(request, f"{obj} restored.")
    return redirect("system:trash")
