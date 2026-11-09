"""
Backup and restore (FR-BAK-1..4).

A backup is one `.tar.gz` holding:

* `database.dump` — `pg_dump --format=custom`, restorable with `pg_restore`;
* `media/` — every stored file and thumbnail;
* `manifest.json` — Trammel version, time, and the applied migrations, which is
  how a restore tells an older backup (migrated forward afterwards) from a newer
  one (refused, FR-BAK-3).

`pg_dump` must be at least the server's major version; the Docker image carries
PostgreSQL 18's client.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from trammel.core import audit, secrets
from trammel.core.formats import show

from . import s3, smb
from .models import Backup, BackupSettings

#: Backup files this module wrote; pruning never touches anything else.
NAME_PREFIX, NAME_SUFFIX = "trammel-", ".tar.gz"


class RestoreRefused(Exception):
    pass


def backup_dir() -> Path:
    path = Path(settings.BACKUP_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def applied_migrations() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for app, name in MigrationRecorder(connection).applied_migrations():
        out.setdefault(app, []).append(name)
    return {app: sorted(names) for app, names in sorted(out.items())}


def known_migrations() -> dict[str, set[str]]:
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    out: dict[str, set[str]] = {}
    for app, name in loader.disk_migrations:
        out.setdefault(app, set()).add(name)
    return out


def check_compatible(manifest: dict) -> list[str]:
    """Migrations in the backup that this code does not have — a newer schema."""
    known = known_migrations()
    return sorted(
        f"{app}.{name}"
        for app, names in manifest.get("migrations", {}).items()
        for name in names
        if name not in known.get(app, set())
    )


def _pg_env() -> dict:
    db = settings.DATABASES["default"]
    env = dict(os.environ)
    env["PGPASSWORD"] = db["PASSWORD"]
    return env


def _pg_args() -> list[str]:
    db = settings.DATABASES["default"]
    args = ["--host", db["HOST"] or "localhost", "--username", db["USER"], "--dbname", db["NAME"]]
    if db.get("PORT"):
        args += ["--port", str(db["PORT"])]
    return args


def _archive_name(kind: str) -> str:
    return f"{NAME_PREFIX}{timezone.now():%Y%m%d-%H%M%S}-{kind}{NAME_SUFFIX}"


def write_archive(target: Path, kind: str, *, version: str | None = None) -> None:
    """Dump the database and files into `target`. Uses no models, so it also works
    before migrations have brought the schema up to this code's version."""
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "database.dump"
        pg_dump = shutil.which(settings.PG_DUMP) or settings.PG_DUMP
        subprocess.run(  # noqa: S603 — fixed argument list, no shell
            [pg_dump, "--format=custom", "--no-owner", "--file", str(dump), *_pg_args()],
            env=_pg_env(),
            check=True,
            capture_output=True,
            timeout=3600,
        )
        manifest = {
            "app": "trammel",
            "version": version or settings.APP_VERSION,
            "created_at": timezone.now().isoformat(),
            "kind": kind,
            "migrations": applied_migrations(),
        }
        (Path(tmp) / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        with tarfile.open(target, "w:gz") as tar:
            tar.add(Path(tmp) / "manifest.json", arcname="manifest.json")
            tar.add(dump, arcname="database.dump")
            media = Path(settings.MEDIA_ROOT)
            for sub in ("blobs", "thumbs"):
                if (media / sub).exists():
                    tar.add(media / sub, arcname=f"media/{sub}")


def pending_migrations() -> tuple[list[str], bool]:
    """Migrations this code would apply, and whether the database is brand new."""
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    pending = [f"{m.app_label}.{m.name}" for m, backwards in plan if not backwards]
    return pending, not executor.loader.applied_migrations


def pre_migration_archive() -> Path | None:
    """Back up before migrating an existing database (FR-BAK-4).

    Runs at container start, before `migrate`, so it writes the archive only;
    `record_archive()` adds the Backup row once the schema matches the code.
    None when there is nothing to migrate or nothing yet to protect. Raises if
    the backup cannot be written.
    """
    pending, fresh = pending_migrations()
    if not pending or fresh:
        return None
    target = backup_dir() / _archive_name(Backup.Kind.PRE_UPDATE)
    try:
        write_archive(target, Backup.Kind.PRE_UPDATE, version=f"before {settings.APP_VERSION}")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def record_archive(path: Path, kind: str) -> Backup:
    """Register an archive written by `write_archive`, copy it off-site and prune."""
    copies = _copy_offsite(path)
    record = Backup.objects.create(
        kind=kind,
        status=Backup.Status.OK,
        filename=path.name,
        size=path.stat().st_size,
        **copies.fields(),
    )
    audit.record(
        "backup.create",
        entity_type="system.backup",
        entity_id=str(record.pk),
        summary=f"{record.get_kind_display()} backup taken ({path.name})",
    )
    prune()
    return record


def create(kind: str = Backup.Kind.MANUAL) -> Backup:
    record = Backup.objects.create(kind=kind)
    name = _archive_name(kind)
    target = backup_dir() / name
    try:
        write_archive(target, kind)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = exc.stderr.decode(errors="replace") if getattr(exc, "stderr", None) else str(exc)
        target.unlink(missing_ok=True)
        Backup.objects.filter(pk=record.pk).update(
            status=Backup.Status.FAILED, error=detail[-4000:]
        )
        record.refresh_from_db()
        _failed(record)
        return record
    copies = _copy_offsite(target)
    Backup.objects.filter(pk=record.pk).update(
        status=Backup.Status.OK,
        filename=name,
        size=target.stat().st_size,
        **copies.fields(),
    )
    record.refresh_from_db()
    audit.record(
        "backup.create",
        entity_type="system.backup",
        entity_id=str(record.pk),
        summary=f"{record.get_kind_display()} backup taken ({name})",
    )
    prune()
    return record


def s3_target() -> s3.Target | None:
    """The configured S3 copy, or None when it is off or incomplete."""
    cfg = BackupSettings.load()
    secret = secrets.get(BackupSettings.S3_SECRET)
    if not (cfg.s3_enabled and cfg.s3_endpoint and cfg.s3_access_key and secret):
        return None
    return s3.Target(
        endpoint=cfg.s3_endpoint,
        region=cfg.s3_region or "us-east-1",
        bucket=cfg.s3_bucket,
        access_key=cfg.s3_access_key,
        secret_key=secret,
        prefix=cfg.s3_prefix,
    )


def smb_target() -> smb.Target | None:
    """The configured network share, or None when it is off or incomplete."""
    cfg = BackupSettings.load()
    if not (cfg.smb_enabled and cfg.smb_server.strip() and cfg.smb_share.strip()):
        return None
    return smb.Target(
        server=cfg.smb_server.strip(),
        share=cfg.smb_share.strip().strip("\\/"),
        folder=cfg.smb_folder.strip(),
        username=cfg.smb_username.strip(),
        password=secrets.get(BackupSettings.SMB_SECRET) or "",
    )


def offbox_folder() -> Path | None:
    cfg = BackupSettings.load()
    if cfg.offbox_enabled and (folder := cfg.offbox_path.strip()):
        return Path(folder)
    return None


def _outbound_allowed() -> bool:
    from trammel.core.models import SystemSettings

    return not SystemSettings.load().is_offline


@dataclass
class Copies:
    """Where a backup was copied, and what went wrong."""

    offbox: bool = False
    smb: bool = False
    s3: bool = False
    errors: list[str] = field(default_factory=list)

    def fields(self) -> dict:
        return {
            "offbox": self.offbox,
            "smb": self.smb,
            "s3": self.s3,
            "copy_error": "\n".join(self.errors),
        }


def _copy_offsite(path: Path) -> Copies:
    """Copy to each off-box target that is switched on. One failing does not stop the rest."""
    cfg = BackupSettings.load()
    done = Copies()
    if cfg.offbox_enabled:
        folder = offbox_folder()
        if folder is None:
            done.errors.append("Folder: no folder is set.")
        else:
            try:
                folder.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, folder / path.name)
                done.offbox = True
            except OSError as exc:
                done.errors.append(f"Folder: {exc}")
    if cfg.smb_enabled:
        share = smb_target()
        if share is None:
            done.errors.append("Network share: the server or share name is missing.")
        else:
            try:
                smb.put_file(share, path)
                done.smb = True
            except smb.SMBError as exc:
                done.errors.append(f"Network share: {exc}")
    if cfg.s3_enabled:
        target = s3_target()
        if target is None:
            done.errors.append("S3: the endpoint, access key or secret key is missing.")
        elif not _outbound_allowed():
            done.errors.append("S3: skipped because outbound traffic is turned off.")
        else:
            try:
                s3.put_file(target, path)
                done.s3 = True
            except s3.S3Error as exc:
                done.errors.append(f"S3: {exc}")
    if done.errors:
        audit.record(
            "backup.copy_failed",
            entity_type="system.backup",
            entity_id=path.name,
            summary="An off-site backup copy failed",
            after={"errors": done.errors},
        )
    return done


def test_smb() -> str:
    """Write, list and delete a small file on the share; return what happened."""
    share = smb_target()
    if share is None:
        return "Fill in the server and share, and turn the network share copy on."
    name = f"trammel-connection-test-{timezone.now():%Y%m%d%H%M%S}.txt"
    try:
        smb.put_bytes(share, name, b"Trammel can write here.\n")
        smb.list_names(share, "trammel-connection-test-")
        smb.delete(share, name)
    except smb.SMBError as exc:
        return f"Failed: {exc}"
    return f"OK — Trammel can write, list and delete in {share.path()}."


def test_s3() -> str:
    """Write, list and delete a small object; return what happened."""
    target = s3_target()
    if target is None:
        return "Fill in the endpoint, access key and secret key, and turn the copy on."
    if not _outbound_allowed():
        return "Outbound traffic is turned off in System settings."
    name = f"trammel-connection-test-{timezone.now():%Y%m%d%H%M%S}.txt"
    try:
        key = s3.put_bytes(target, name, b"Trammel can write here.\n")
        s3.list_keys(target, "trammel-connection-test-")
        s3.delete(target, key)
    except s3.S3Error as exc:
        return f"Failed: {exc}"
    return "OK — Trammel can write, list and delete in that bucket."


def _failed(record: Backup) -> None:
    audit.record(
        "backup.failed",
        entity_type="system.backup",
        entity_id=str(record.pk),
        summary="Backup failed",
        after={"error": record.error[-500:]},
    )
    try:
        from trammel.notifications.services import notify
    except ImportError:  # pragma: no cover — notifications not installed
        return
    notify(
        "backup_failed",
        context={
            "when": show(record.created_at, "dt"),
            "error": record.error[-2000:],
        },
    )


def _ours(name: str) -> bool:
    return name.startswith(NAME_PREFIX) and name.endswith(NAME_SUFFIX)


def prune() -> int:
    """Keep the newest `keep` backups here and in every off-box copy."""
    cfg = BackupSettings.load()
    keep = cfg.keep or 1
    old = list(Backup.objects.filter(status=Backup.Status.OK).order_by("-created_at")[keep:])
    for record in old:
        (backup_dir() / record.filename).unlink(missing_ok=True)
        record.delete()
    # Names sort by time (trammel-YYYYmmdd-HHMMSS-kind), so the oldest come first.
    if (folder := offbox_folder()) is not None:
        try:
            names = sorted(p.name for p in folder.iterdir() if _ours(p.name))
            for name in names[:-keep]:
                (folder / name).unlink(missing_ok=True)
        except OSError:
            pass
    if (share := smb_target()) is not None:
        try:
            names = [n for n in smb.list_names(share, NAME_PREFIX) if _ours(n)]
            for name in names[:-keep]:
                smb.delete(share, name)
        except smb.SMBError:
            pass  # the next backup tries again
    target = s3_target()
    if target is not None and _outbound_allowed():
        try:
            keys = [k for k in s3.list_keys(target, NAME_PREFIX) if _ours(k.rsplit("/", 1)[-1])]
            for key in keys[:-keep]:
                s3.delete(target, key)
        except s3.S3Error:
            pass  # the next backup tries again
    return len(old)


def last_success() -> Backup | None:
    return Backup.objects.filter(status=Backup.Status.OK).first()


def age_warning() -> str:
    """The banner text when backups are stale or failing (FR-BAK-2), else ''."""
    settings_ = BackupSettings.load()
    last = last_success()
    if last is None:
        return "No backup has been taken yet."
    age = timezone.now() - last.created_at
    if age > timedelta(days=settings_.warn_after_days):
        return f"The last successful backup is {age.days} days old."
    latest = Backup.objects.first()
    if latest and latest.status == Backup.Status.FAILED:
        return "The most recent backup failed."
    return ""


def read_manifest(path: Path) -> dict:
    with tarfile.open(path, "r:gz") as tar:
        member = tar.extractfile("manifest.json")
        if member is None:
            raise RestoreRefused("This file has no manifest; it is not a Trammel backup.")
        return json.loads(member.read())


def restore(path: Path) -> None:
    """Restore a backup (FR-BAK-3). Refuses a newer schema; migrates an older one forward.

    Takes a backup of the current state first, so a mistaken restore can be undone.
    """
    from django.core.management import call_command

    manifest = read_manifest(path)
    newer = check_compatible(manifest)
    if newer:
        raise RestoreRefused(
            "This backup is from a newer version of Trammel (it has migrations this version "
            f"does not: {', '.join(newer[:5])}). Update Trammel first."
        )
    create(Backup.Kind.PRE_RESTORE)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(path, "r:gz") as tar:
            tar.extractall(tmp, filter="data")
        pg_restore = shutil.which(settings.PG_RESTORE) or settings.PG_RESTORE
        connection.close()
        subprocess.run(  # noqa: S603 — fixed argument list, no shell
            [
                pg_restore,
                "--clean",
                "--if-exists",
                "--no-owner",
                "--single-transaction",
                *_pg_args(),
                str(Path(tmp) / "database.dump"),
            ],
            env=_pg_env(),
            check=True,
            capture_output=True,
            timeout=3600,
        )
        media_src = Path(tmp) / "media"
        if media_src.exists():
            for sub in ("blobs", "thumbs"):
                if (media_src / sub).exists():
                    shutil.copytree(
                        media_src / sub, Path(settings.MEDIA_ROOT) / sub, dirs_exist_ok=True
                    )
    call_command("migrate", interactive=False, verbosity=0)
    audit.record(
        "backup.restore",
        entity_type="system.backup",
        summary=f"Restored the backup {path.name} (from version {manifest.get('version', '?')})",
    )
