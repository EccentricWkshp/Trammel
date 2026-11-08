from django.core.management.base import BaseCommand, CommandError

from trammel.core import audit
from trammel.core.access import SYSTEM
from trammel.system import backup


class Command(BaseCommand):
    help = "Take a backup now (database and files) into BACKUP_DIR."

    def handle(self, *args, **options):
        with audit.acting_as(SYSTEM, source="cli"):
            record = backup.create("manual")
        if record.status != "ok":
            raise CommandError(f"Backup failed: {record.error}")
        self.stdout.write(self.style.SUCCESS(f"Backup written: {record.filename}"))
