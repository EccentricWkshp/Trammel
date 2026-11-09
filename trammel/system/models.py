"""Backups, exports and the update check (FR-BAK-1..5, SPEC §4.3)."""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from trammel.core.models import SingletonModel, uuid7


class BackupSettings(SingletonModel):
    scheduled = models.BooleanField("take backups automatically", default=True)
    interval_hours = models.PositiveSmallIntegerField(default=24)
    keep = models.PositiveSmallIntegerField(
        "backups to keep",
        default=14,
        help_text="Here and in every copy below; older ones are deleted.",
    )
    warn_after_days = models.PositiveSmallIntegerField(
        "warn when the last backup is older than (days)", default=7
    )

    # Off-box copies (FR-BAK-1). Each is optional and has its own switch; the
    # settings page shows a target's fields only while it is switched on.

    # A folder the app can already write to: a network share mounted on the host
    # and into the container, or a second disk.
    offbox_enabled = models.BooleanField("copy backups to a folder", default=False)
    offbox_path = models.CharField(
        "folder",
        max_length=500,
        blank=True,
        help_text="A path inside the app container, such as a network share mounted on the "
        "host and into the container in docker-compose.yml.",
    )

    # A network share reached directly over SMB. The password is a Credential.
    smb_enabled = models.BooleanField("copy backups to a network share", default=False)
    smb_server = models.CharField(
        "server", max_length=200, blank=True, help_text="Host name or IP address, e.g. nas.local."
    )
    smb_share = models.CharField("share", max_length=200, blank=True, help_text="e.g. backups")
    smb_folder = models.CharField(
        "folder on the share",
        max_length=300,
        blank=True,
        default="trammel",
        help_text="Created if it does not exist. Leave blank for the top of the share.",
    )
    smb_username = models.CharField(
        "user name",
        max_length=150,
        blank=True,
        help_text="For a domain account, DOMAIN\\name.",
    )

    # S3-compatible off-site copy (FR-BAK-1). The secret key is a Credential.
    s3_enabled = models.BooleanField("copy backups to S3-compatible storage", default=False)
    s3_endpoint = models.URLField(
        "endpoint",
        blank=True,
        help_text="e.g. https://s3.us-west-2.amazonaws.com, https://s3.us-west-004.backblazeb2.com "
        "or your R2 / MinIO address.",
    )
    s3_region = models.CharField("region", max_length=40, blank=True, default="us-east-1")
    s3_bucket = models.CharField(
        "bucket",
        max_length=100,
        blank=True,
        help_text="Leave blank if the endpoint address already includes the bucket.",
    )
    s3_prefix = models.CharField(
        "folder in the bucket", max_length=200, blank=True, default="trammel"
    )
    s3_access_key = models.CharField("access key ID", max_length=128, blank=True)

    S3_SECRET = "backup.s3_secret_key"
    SMB_SECRET = "backup.smb_password"

    class Meta:
        verbose_name = "backup settings"

    def __str__(self) -> str:
        return "Backup settings"


class Backup(models.Model):
    class Kind(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        MANUAL = "manual", "Manual"
        PRE_UPDATE = "pre_update", "Before an update"
        PRE_RESTORE = "pre_restore", "Before a restore"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        OK = "ok", "Succeeded"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    created_at = models.DateTimeField(default=timezone.now)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.RUNNING)
    filename = models.CharField(max_length=200, blank=True)
    size = models.BigIntegerField(default=0)
    error = models.TextField(blank=True)
    offbox = models.BooleanField(default=False)
    smb = models.BooleanField(default=False)
    s3 = models.BooleanField(default=False)
    #: Why an off-site copy failed; the backup itself still succeeded.
    copy_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} backup {self.created_at:%Y-%m-%d %H:%M}"


class Export(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    created_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=8, default="running")
    filename = models.CharField(max_length=200, blank=True)
    size = models.BigIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Export {self.created_at:%Y-%m-%d %H:%M}"


class UpdateStatus(SingletonModel):
    checked_at = models.DateTimeField(null=True, blank=True)
    latest_version = models.CharField(max_length=40, blank=True)
    release_url = models.URLField(blank=True)
    error = models.CharField(max_length=300, blank=True)
    notified_version = models.CharField(max_length=40, blank=True)

    def __str__(self) -> str:
        return "Update status"
