"""Backup copies on a network share over SMB, and the off-box settings page (FR-BAK-1).

The SMB client is replaced by one backed by a temporary folder, so the copy,
listing, pruning and failure handling run for real without a server.
"""

import datetime as dt
import importlib
from pathlib import Path

import pytest
import smbclient
from django.apps import apps

from tests.test_system import fake_pg_dump  # noqa: F401 — fixture
from trammel.core import secrets
from trammel.system import backup, smb
from trammel.system.models import Backup, BackupSettings


class FakeShare:
    """smbclient's functions, writing under a local folder instead of a server."""

    def __init__(self, root: Path):
        self.root = root
        self.sessions = []
        self.fail = None

    def local(self, unc: str) -> Path:
        parts = [p for p in unc.split("\\") if p]
        return self.root.joinpath(*parts)

    def _check(self, options):
        self.sessions.append((options["username"], options["password"]))
        if self.fail:
            raise self.fail

    def makedirs(self, path, exist_ok=False, **options):
        self._check(options)
        self.local(path).mkdir(parents=True, exist_ok=exist_ok)

    def open_file(self, path, mode="r", **options):
        self._check(options)
        target = self.local(path)
        if not target.parent.exists():
            raise OSError(f"[NtStatus 0xc000003a] Path not found: {path}")
        return open(target, mode)  # noqa: SIM115 — the caller closes it

    def replace(self, src, dst, **options):
        self._check(options)
        self.local(src).replace(self.local(dst))

    def listdir(self, path, **options):
        self._check(options)
        return [p.name for p in self.local(path).iterdir()]

    def remove(self, path, **options):
        self._check(options)
        self.local(path).unlink()

    def reset_connection_cache(self, **options):
        pass


@pytest.fixture
def share(monkeypatch, tmp_path):
    fake = FakeShare(tmp_path / "net")
    (fake.root / "nas" / "backups").mkdir(parents=True)
    for name in ("makedirs", "open_file", "replace", "listdir", "remove", "reset_connection_cache"):
        monkeypatch.setattr(smbclient, name, getattr(fake, name))
    cfg = BackupSettings.load()
    cfg.smb_enabled, cfg.smb_server, cfg.smb_share = True, "nas", "backups"
    cfg.smb_folder, cfg.smb_username = "shop/trammel", "SHOP\\backup"
    cfg.save()
    secrets.put(BackupSettings.SMB_SECRET, "hunter2")
    return fake


def test_unc_paths():
    target = smb.Target("nas.local", "backups", "shop/trammel/", "", "")
    assert target.path() == r"\\nas.local\backups\shop\trammel"
    assert target.path("a.tar.gz") == r"\\nas.local\backups\shop\trammel\a.tar.gz"
    assert smb.Target("nas", "b", "", "", "").path("x") == r"\\nas\b\x"


@pytest.mark.django_db
def test_backup_is_copied_to_the_share_and_pruned(setup_done, fake_pg_dump, share, monkeypatch):  # noqa: F811
    folder = share.root / "nas" / "backups" / "shop" / "trammel"
    cfg = BackupSettings.load()
    cfg.keep = 1
    cfg.save()
    for day in (1, 2):
        when = dt.datetime(2026, 11, day, tzinfo=dt.UTC)
        monkeypatch.setattr(backup.timezone, "now", lambda when=when: when)
        record = backup.create("manual")
        assert record.status == "ok" and record.smb and not record.copy_error
    (folder / "family-photos.zip").write_bytes(b"not ours")
    backup.prune()
    # Only the newest backup is kept; no half-written copy is left; other files stay.
    assert sorted(p.name for p in folder.iterdir()) == [
        "family-photos.zip",
        "trammel-20261102-000000-manual.tar.gz",
    ]
    assert share.sessions[0] == ("SHOP\\backup", "hunter2")


@pytest.mark.django_db
def test_a_failing_share_does_not_stop_the_backup_or_other_copies(
    setup_done,
    fake_pg_dump,  # noqa: F811
    share,
    tmp_path,
):
    share.fail = OSError("[Errno 113] No route to host")
    cfg = BackupSettings.load()
    cfg.offbox_enabled, cfg.offbox_path = True, str(tmp_path / "usb")
    cfg.save()
    record = backup.create("manual")
    assert record.status == "ok" and record.offbox and not record.smb
    assert record.copy_error == "Network share: [Errno 113] No route to host"
    assert (tmp_path / "usb" / record.filename).exists()


@pytest.mark.django_db
def test_share_is_local_so_outbound_switch_does_not_stop_it(
    setup_done,
    fake_pg_dump,  # noqa: F811
    share,
):
    from trammel.core.models import SystemSettings

    system = SystemSettings.load()
    system.offline_mode = True
    system.save()
    record = backup.create("manual")
    assert record.smb and not record.copy_error
    assert backup.test_smb().startswith("OK")


@pytest.mark.django_db
def test_connection_test(admin_client, share):
    response = admin_client.post("/settings/maintenance/backup/test-smb/", follow=True)
    assert r"OK — Trammel can write, list and delete in \\nas\backups\shop\trammel" in (
        response.content.decode()
    )
    assert list((share.root / "nas" / "backups" / "shop" / "trammel").iterdir()) == []
    share.fail = OSError("STATUS_LOGON_FAILURE")
    response = admin_client.post("/settings/maintenance/backup/test-smb/", follow=True)
    assert "Failed: STATUS_LOGON_FAILURE" in response.content.decode()


@pytest.mark.django_db
def test_folder_switched_off_is_not_used(setup_done, fake_pg_dump, tmp_path):  # noqa: F811
    cfg = BackupSettings.load()
    cfg.offbox_enabled, cfg.offbox_path = False, str(tmp_path / "usb")
    cfg.save()
    record = backup.create("manual")
    assert not record.offbox and not (tmp_path / "usb").exists()


def _settings_page(admin_client):
    return admin_client.get("/settings/maintenance/").content.decode()


def _group(page, switch):
    start = page.index(f'data-shown-by="id_{switch}"')
    return page[start : page.index(">", start)]


@pytest.mark.django_db
def test_settings_show_a_targets_fields_only_while_it_is_on(admin_client):
    page = _settings_page(admin_client)
    for switch in ("smb_enabled", "s3_enabled", "offbox_enabled"):
        assert "hidden" in _group(page, switch)
    cfg = BackupSettings.load()
    cfg.smb_enabled, cfg.smb_server, cfg.smb_share = True, "nas", "backups"
    cfg.save()
    page = _settings_page(admin_client)
    assert "hidden" not in _group(page, "smb_enabled")
    assert "hidden" in _group(page, "s3_enabled")


@pytest.mark.django_db
def test_share_settings_save_and_require_what_they_need(admin_client):
    base = {"scheduled": "on", "interval_hours": "24", "keep": "14", "warn_after_days": "7"}
    response = admin_client.post("/settings/maintenance/", {**base, "smb_enabled": "on"})
    page = response.content.decode()
    assert response.status_code == 200 and page.count("Needed while this copy") == 2
    # Ticking the switch and saving with nothing filled in shows the fields to fill.
    assert "hidden" not in _group(page, "smb_enabled")

    data = {
        **base,
        "smb_enabled": "on",
        "smb_server": "nas",
        "smb_share": "backups",
        "smb_folder": "trammel",
        "smb_username": "backup",
        "smb_password": "pw1",
    }
    assert admin_client.post("/settings/maintenance/", data).status_code == 302
    assert secrets.get(BackupSettings.SMB_SECRET) == "pw1"
    admin_client.post("/settings/maintenance/", {**data, "smb_password": ""})
    assert secrets.get(BackupSettings.SMB_SECRET) == "pw1"  # blank keeps it
    admin_client.post(
        "/settings/maintenance/", {**data, "smb_password": "", "smb_clear_password": "on"}
    )
    assert not secrets.is_set(BackupSettings.SMB_SECRET)
    assert Backup.objects.count() == 0


@pytest.mark.django_db
def test_migration_switches_on_a_folder_already_in_use():
    migration = importlib.import_module("trammel.system.migrations.0003_offbox_targets")
    cfg = BackupSettings.load()
    cfg.offbox_enabled, cfg.offbox_path = False, "/mnt/nas"
    cfg.save()
    migration.switch_on_existing_folder(apps, None)
    assert BackupSettings.load().offbox_enabled


def test_errors_are_put_in_words():
    assert smb._describe(OSError("")) == "OSError"
    assert len(smb._describe(ValueError("x" * 1000))) == 300
