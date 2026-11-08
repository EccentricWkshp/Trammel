from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from trammel.core import audit
from trammel.core.access import SYSTEM
from trammel.system import backup


class Command(BaseCommand):
    help = (
        "Restore a Trammel backup. The current data is backed up first. "
        "A backup from a newer version is refused; an older one is migrated forward."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", help="Path to a trammel-*.tar.gz backup.")
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")

    def handle(self, *args, file, yes=False, **options):
        path = Path(file)
        if not path.exists():
            raise CommandError(f"No such file: {path}")
        try:
            manifest = backup.read_manifest(path)
        except backup.RestoreRefused as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"Backup from {manifest.get('created_at')} (Trammel {manifest.get('version')})."
        )
        if (
            not yes
            and input("This replaces ALL current data. Type RESTORE to continue: ") != "RESTORE"
        ):
            raise CommandError("Cancelled.")
        with audit.acting_as(SYSTEM, source="cli"):
            try:
                backup.restore(path)
            except backup.RestoreRefused as exc:
                raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS("Restored."))
