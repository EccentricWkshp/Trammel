"""FR-BAK-1..5, the update check (§4.3) and the trash (FR-ADM-9)."""

import datetime as dt
import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from trammel.customers.models import Customer
from trammel.notifications.models import EmailLog
from trammel.system import backup, export, trash, updates
from trammel.system.models import Backup, BackupSettings, UpdateStatus


@pytest.fixture
def fake_pg_dump(monkeypatch):
    def run(args, **kwargs):
        target = Path(args[args.index("--file") + 1])
        target.write_bytes(b"PGDMP fake dump")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(backup.subprocess, "run", run)


@pytest.mark.django_db
def test_backup_archive_contents(setup_done, admin, fake_pg_dump, tmp_path):
    from trammel.files.services import upload

    upload(SimpleUploadedFile("d.pdf", b"%PDF-1.4 drawing"), name="d.pdf", user=admin)
    settings = BackupSettings.load()
    settings.offbox_enabled, settings.offbox_path = True, str(tmp_path / "nas")
    settings.save()
    record = backup.create("manual")
    assert record.status == "ok" and record.offbox
    path = backup.backup_dir() / record.filename
    with tarfile.open(path) as tar:
        names = tar.getnames()
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert "database.dump" in names and any(n.startswith("media/blobs") for n in names)
    assert manifest["app"] == "trammel" and "core" in manifest["migrations"]
    assert (tmp_path / "nas" / record.filename).exists()
    assert backup.check_compatible(manifest) == []


@pytest.mark.django_db
def test_newer_backup_is_refused(setup_done, fake_pg_dump):
    record = backup.create("manual")
    path = backup.backup_dir() / record.filename
    manifest = backup.read_manifest(path)
    manifest["migrations"]["core"].append("0999_from_the_future")
    assert backup.check_compatible(manifest) == ["core.0999_from_the_future"]
    newer = path.with_name("newer.tar.gz")
    with tarfile.open(newer, "w:gz") as tar:
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        import io

        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(backup.RestoreRefused, match="newer version"):
        backup.restore(newer)


@pytest.mark.django_db
def test_retention(setup_done, fake_pg_dump):
    settings = BackupSettings.load()
    settings.keep = 2
    settings.save()
    for _ in range(3):
        backup.create("manual")
    assert Backup.objects.filter(status="ok").count() == 2


@pytest.mark.django_db
def test_failure_is_recorded_announced_and_shown(
    admin, admin_client, settings, django_capture_on_commit_callbacks
):
    settings.PG_DUMP = "definitely-not-pg-dump"
    with django_capture_on_commit_callbacks(execute=True):
        record = backup.create("scheduled")
    assert record.status == "failed" and record.error
    assert EmailLog.objects.filter(event="backup_failed").exists()
    page = admin_client.get("/").content.decode()
    assert "No backup has been taken yet." in page


@pytest.mark.django_db
def test_age_warning(setup_done, fake_pg_dump):
    record = backup.create("manual")
    assert backup.age_warning() == ""
    Backup.objects.filter(pk=record.pk).update(created_at=timezone.now() - dt.timedelta(days=9))
    assert "9 days old" in backup.age_warning()


@pytest.mark.django_db
def test_full_export(setup_done, admin, customer):
    from trammel.files.services import upload

    upload(SimpleUploadedFile("plan.pdf", b"%PDF-1.4 plan"), name="plan.pdf", user=admin)
    record = export.build()
    assert record.status == "ok"
    with zipfile.ZipFile(Path(backup.backup_dir()) / "exports" / record.filename) as zf:
        names = zf.namelist()
        users = zf.read("data/accounts.user.ndjson").decode()
        customers = zf.read("data/customers.customer.ndjson").decode()
        assert "README.md" in names
        assert any(n.startswith("files/") and n.endswith("plan.pdf") for n in names)
        assert "data/core.credential.ndjson" not in names
    assert "admin@example.com" in users and "password" not in users
    assert "Grace Hopper" in customers


@pytest.mark.django_db
def test_update_check(admin, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        status = updates.check(fetch=lambda: ("v99.0.0", "https://example.com/r"))
        updates.check(fetch=lambda: ("v99.0.0", "https://example.com/r"))
    assert status.latest_version == "v99.0.0" and updates.available() == "v99.0.0"
    assert EmailLog.objects.filter(event="update_available").count() == 1
    assert updates.is_newer("1.2.10", "1.2.9") and not updates.is_newer("0.1.0", "0.1.0.dev0")


@pytest.mark.django_db
def test_updates_live_on_the_system_page(admin_client, monkeypatch):
    status = UpdateStatus.load()
    status.latest_version, status.release_url = "v99.0.0", "https://example.com/r"
    status.save()

    system_page = admin_client.get("/settings/system/").content.decode()
    assert "Check now" in system_page and "Trammel v99.0.0</strong> is available" in system_page
    assert "once a day" in system_page
    assert "Check now" not in admin_client.get("/settings/maintenance/").content.decode()
    # The banner sends the admin to the same place.
    assert 'href="/settings/system/"' in admin_client.get("/").content.decode()

    monkeypatch.setattr(updates, "fetch_latest", lambda: ("v99.0.0", "https://example.com/r"))
    monkeypatch.setattr(updates.check, "__defaults__", (updates.fetch_latest,))
    response = admin_client.post("/settings/maintenance/updates/")
    assert response.status_code == 302 and response.url == "/settings/system/"


@pytest.mark.django_db
def test_update_check_respects_switches(setup_done):
    from trammel.core.models import SystemSettings

    system = SystemSettings.load()
    system.offline_mode = True
    system.save()
    called = []
    updates.check(fetch=lambda: called.append(1) or ("v9.9.9", ""))
    assert not called and "off" in UpdateStatus.load().error


@pytest.mark.django_db
def test_trash_restore_and_purge(setup_done, customer):
    customer.delete()
    rows = trash.items()
    assert rows and rows[0]["obj"] == customer
    trash.restore("customers.customer", customer.pk)
    assert Customer.objects.filter(pk=customer.pk).exists()
    customer.delete()
    Customer.all_objects.filter(pk=customer.pk).update(
        deleted_at=timezone.now() - dt.timedelta(days=31)
    )
    assert trash.purge() >= 1
    assert not Customer.all_objects.filter(pk=customer.pk).exists()


@pytest.mark.django_db
def test_screens(admin_client, customer, fake_pg_dump):
    record = backup.create("manual")
    customer.delete()
    for path in (
        "/settings/maintenance/",
        "/settings/maintenance/trash/",
        f"/settings/maintenance/backup/{record.pk}/restore/",
    ):
        assert admin_client.get(path).status_code == 200, path
    response = admin_client.post(
        f"/settings/maintenance/trash/customers.customer/{customer.pk}/restore/"
    )
    assert response.status_code == 302 and Customer.objects.filter(pk=customer.pk).exists()


@pytest.mark.django_db
def test_notice_banner_does_not_break_the_settings_layout(admin_client):
    """The banner sits in the full-width messages box, not as a stray grid cell."""
    page = admin_client.get("/settings/maintenance/").content.decode()
    box = page.index('<div class="messages"')
    assert box < page.index("No backup has been taken yet.") < page.index('<nav class="sidebar"')
    main = page[page.index('<main id="main"') :]
    children = main.split(">", 1)[1].lstrip()
    assert children.startswith('<div class="messages"')
    assert page.count("No backup has been taken yet.") == 1


@pytest.mark.django_db
def test_maintenance_is_admin_only(staff_client):
    assert staff_client.get("/settings/maintenance/").status_code == 403
