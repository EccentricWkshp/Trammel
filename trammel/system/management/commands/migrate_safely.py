"""Migrate at container start, backing up an existing database first (FR-BAK-4).

`docker compose pull && docker compose up -d` is how a Docker install updates,
so the backup before an update has to happen here: when the new image brings
migrations, the database is dumped before any of them run. A fresh install has
nothing to protect and is migrated straight away.

If the backup fails, nothing is migrated and the container stops with the
reason, so an update never runs unprotected. Set TRAMMEL_SKIP_PRE_UPDATE_BACKUP=1
to migrate anyway (for example when the backup volume is full and you have
another copy).
"""

from __future__ import annotations

import os
import subprocess

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from trammel.system import backup
from trammel.system.models import Backup


class Command(BaseCommand):
    help = "Back up an existing database if migrations are pending, then migrate."

    def handle(self, *args, **options):
        skip = os.environ.get("TRAMMEL_SKIP_PRE_UPDATE_BACKUP", "") in ("1", "true", "yes")
        archive = None
        if not skip:
            try:
                archive = backup.pre_migration_archive()
            except (OSError, subprocess.SubprocessError) as exc:
                detail = getattr(exc, "stderr", None)
                detail = detail.decode(errors="replace") if detail else str(exc)
                raise CommandError(
                    "The backup before migrating failed, so nothing was migrated:\n"
                    f"{detail.strip()[-2000:]}\n"
                    "Fix the cause, or set TRAMMEL_SKIP_PRE_UPDATE_BACKUP=1 to migrate "
                    "without it."
                ) from exc
            if archive:
                self.stdout.write(f"Backed up before migrating: {archive.name}")
        call_command("migrate", interactive=False, verbosity=options["verbosity"])
        if archive:
            backup.record_archive(archive, Backup.Kind.PRE_UPDATE)
