from __future__ import annotations

from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from trammel.core import audit, secrets, tasks
from trammel.core.services import save_audited
from trammel.core.web import admin_required

from . import backup, trash, updates
from .models import Backup, BackupSettings, Export, UpdateStatus


class BackupSettingsForm(forms.ModelForm):
    s3_secret_key = forms.CharField(
        label="secret access key",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Stored encrypted and never shown again. Leave blank to keep the current one.",
    )
    s3_clear_secret = forms.BooleanField(label="forget the saved secret key", required=False)

    class Meta:
        model = BackupSettings
        fields = [
            "scheduled",
            "interval_hours",
            "keep",
            "warn_after_days",
            "offbox_path",
            "s3_enabled",
            "s3_endpoint",
            "s3_region",
            "s3_bucket",
            "s3_prefix",
            "s3_access_key",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if secrets.is_set(BackupSettings.S3_SECRET):
            self.fields[
                "s3_secret_key"
            ].help_text = "A secret key is saved (encrypted). Leave blank to keep it."
        else:
            del self.fields["s3_clear_secret"]

    def clean(self):
        data = super().clean()
        if data.get("s3_enabled"):
            for name in ("s3_endpoint", "s3_access_key"):
                if not data.get(name):
                    self.add_error(name, "Needed to copy backups to S3.")
            if not data.get("s3_secret_key") and (
                data.get("s3_clear_secret") or not secrets.is_set(BackupSettings.S3_SECRET)
            ):
                self.add_error("s3_secret_key", "Needed to copy backups to S3.")
        return data

    def save_secret(self):
        if self.cleaned_data.get("s3_clear_secret"):
            secrets.clear(BackupSettings.S3_SECRET)
        if value := self.cleaned_data.get("s3_secret_key"):
            secrets.put(BackupSettings.S3_SECRET, value)


@admin_required
def maintenance(request):
    obj = BackupSettings.load()
    before = audit.snapshot(obj)
    form = BackupSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        save_audited(form.save(commit=False), before=before, summary="Edited backup settings")
        form.save_secret()
        messages.success(request, "Backup settings saved.")
        return redirect("system:maintenance")
    return render(
        request,
        "system/maintenance.html",
        {
            "form": form,
            "backups": Backup.objects.all()[:30],
            "exports": Export.objects.all()[:10],
            "last": backup.last_success(),
            "warning": backup.age_warning(),
            "update": UpdateStatus.load(),
            "update_available": updates.available(),
            "version": settings.APP_VERSION,
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
    return redirect("system:maintenance")


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
