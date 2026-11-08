from datetime import timedelta

from django.utils import timezone

from trammel.core.tasks import periodic, task


@periodic("system.backup", every=timedelta(hours=1))
def scheduled_backup(payload):
    """Take a backup when the configured interval has passed (FR-BAK-1)."""
    from . import backup
    from .models import Backup, BackupSettings

    settings = BackupSettings.load()
    if not settings.scheduled:
        return
    last = Backup.objects.exclude(status=Backup.Status.RUNNING).first()
    if last and timezone.now() - last.created_at < timedelta(hours=settings.interval_hours):
        return
    backup.create(Backup.Kind.SCHEDULED)


@task("system.backup_now")
def backup_now(payload):
    from . import backup

    backup.create(payload.get("kind", "manual"))


@task("system.export")
def export(payload):
    from . import export as exporter

    exporter.build()


@periodic("system.update_check", every=timedelta(hours=24))
def update_check(payload):
    from . import updates

    updates.check()


@periodic("system.housekeeping", every=timedelta(hours=24))
def housekeeping(payload):
    """Empty the trash past 30 days; prune sign-in attempts; apply audit retention."""
    from trammel.accounts import security
    from trammel.core.models import AuditEvent, SystemSettings

    from . import trash

    trash.purge()
    security.prune()
    days = SystemSettings.load().audit_retention_days
    if days:
        AuditEvent.purge_before(timezone.now() - timedelta(days=days))
