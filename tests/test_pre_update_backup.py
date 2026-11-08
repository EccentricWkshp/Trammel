"""FR-BAK-4: a Docker update backs up the database before migrating it."""

import subprocess

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from tests.test_system import fake_pg_dump  # noqa: F401 — fixture
from trammel.system import backup
from trammel.system.management.commands import migrate_safely
from trammel.system.models import Backup


@pytest.fixture
def steps(monkeypatch):
    """Record what the command does instead of really migrating."""
    done = []

    def fake_call(name, **kwargs):
        done.append(("migrate", Backup.objects.count()))

    monkeypatch.setattr(migrate_safely, "call_command", fake_call)
    return done


def _pending(monkeypatch, pending, fresh=False):
    monkeypatch.setattr(backup, "pending_migrations", lambda: (pending, fresh))


@pytest.mark.django_db
def test_update_with_migrations_is_backed_up_first(
    setup_done,
    fake_pg_dump,  # noqa: F811
    steps,
    monkeypatch,
):
    _pending(monkeypatch, ["billing.0099_new"])
    call_command("migrate_safely")
    assert steps == [("migrate", 0)]  # migrated before the row exists: no ORM on the old schema
    record = Backup.objects.get()
    assert record.kind == "pre_update" and record.status == "ok"
    path = backup.backup_dir() / record.filename
    manifest = backup.read_manifest(path)
    assert manifest["kind"] == "pre_update" and manifest["version"].startswith("before ")


@pytest.mark.django_db
def test_nothing_to_migrate_or_a_new_install_takes_no_backup(
    setup_done,
    fake_pg_dump,  # noqa: F811
    steps,
    monkeypatch,
):
    _pending(monkeypatch, [])
    call_command("migrate_safely")
    _pending(monkeypatch, ["core.0001_initial"], fresh=True)
    call_command("migrate_safely")
    assert steps == [("migrate", 0), ("migrate", 0)] and not Backup.objects.exists()


@pytest.mark.django_db
def test_a_failed_backup_stops_the_update(setup_done, steps, monkeypatch):
    _pending(monkeypatch, ["billing.0099_new"])

    def broken(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, b"", b"pg_dump: error: disk full")

    monkeypatch.setattr(backup.subprocess, "run", broken)
    with pytest.raises(CommandError, match="disk full"):
        call_command("migrate_safely")
    assert steps == [] and not list(backup.backup_dir().glob("*pre_update*"))

    monkeypatch.setenv("TRAMMEL_SKIP_PRE_UPDATE_BACKUP", "1")
    call_command("migrate_safely")
    assert steps == [("migrate", 0)]


@pytest.mark.django_db
def test_pending_migrations_reports_none_on_a_migrated_database(setup_done):
    assert backup.pending_migrations() == ([], False)
