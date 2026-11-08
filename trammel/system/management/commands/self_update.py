"""Automatic update for source (git) installs (SPEC §4.3).

backup → check out the release tag → install dependencies → migrate → health
check → on any failure, restore the backup and the previous tag. The outcome is
audit-logged. Docker installs must not use this: pull the new image instead.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from trammel.core import audit
from trammel.core.access import SYSTEM
from trammel.system import backup, updates


def _git(*args: str) -> str:
    result = subprocess.run(  # noqa: S603 — fixed program, arguments from this file
        ["git", *args],  # noqa: S607 — git from PATH, as any checkout uses it
        cwd=settings.BASE_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class Command(BaseCommand):
    help = "Update a git checkout to the latest release tag, with backup and rollback."

    def add_arguments(self, parser):
        parser.add_argument("--tag", help="Release tag to install (default: latest release).")

    def handle(self, *args, tag=None, **options):
        if Path("/.dockerenv").exists():
            raise CommandError("This is a Docker install. Pull the new image instead.")
        if not (Path(settings.BASE_DIR) / ".git").exists():
            raise CommandError("Not a git checkout.")
        if _git("status", "--porcelain"):
            raise CommandError("The working tree has local changes; refusing to update.")
        tag = tag or updates.fetch_latest()[0]
        if not tag or not updates.is_newer(tag, settings.APP_VERSION):
            self.stdout.write("Already up to date.")
            return
        previous = _git("rev-parse", "HEAD")
        with audit.acting_as(SYSTEM, source="cli"):
            record = backup.create("pre_update")
            if record.status != "ok":
                raise CommandError(f"Backup failed, not updating: {record.error}")
            try:
                _git("fetch", "--tags", "origin")
                _git("checkout", f"tags/{tag}")
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "-r",  # noqa: S603
                        "requirements.txt",
                    ],
                    cwd=settings.BASE_DIR,
                    check=True,
                )
                call_command("migrate", interactive=False)
                call_command("check", deploy=False)
            except (subprocess.CalledProcessError, Exception) as exc:  # noqa: BLE001
                self.stderr.write(f"Update failed ({exc}); rolling back.")
                _git("checkout", previous)
                backup.restore(Path(settings.BACKUP_DIR) / record.filename)
                audit.record(
                    "update.rolled_back",
                    entity_type="system.update",
                    summary=f"Update to {tag} failed and was rolled back",
                    after={"error": str(exc)[:500]},
                )
                raise CommandError("Update failed and was rolled back.") from exc
            audit.record(
                "update.installed",
                entity_type="system.update",
                summary=f"Updated from {settings.APP_VERSION} to {tag}",
            )
        self.stdout.write(self.style.SUCCESS(f"Updated to {tag}. Restart the application."))
