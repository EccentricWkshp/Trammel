"""Email notifications over a user-configured SMTP server (FR-NTF-1..3)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from trammel.core.models import SingletonModel, uuid7


class SmtpSettings(SingletonModel):
    class Security(models.TextChoices):
        STARTTLS = "starttls", "STARTTLS (usually port 587)"
        SSL = "ssl", "SSL/TLS (usually port 465)"
        NONE = "none", "None (local relay only)"

    host = models.CharField(max_length=200, blank=True)
    port = models.PositiveIntegerField(default=587)
    security = models.CharField(max_length=8, choices=Security.choices, default=Security.STARTTLS)
    username = models.CharField(max_length=200, blank=True)
    from_address = models.EmailField(blank=True)
    from_name = models.CharField(max_length=100, blank=True)
    staff_recipients = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="+",
        help_text="Who gets staff notifications. Nobody chosen sends them to every system admin.",
    )
    overdue_reminder_days = models.PositiveSmallIntegerField(
        default=7, help_text="Remind a customer about an overdue invoice at most this often."
    )

    class Meta:
        verbose_name = "email settings"

    def __str__(self) -> str:
        return "Email settings"

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.from_address)


class EmailTemplate(models.Model):
    """Subject and body for one event, in sandboxed Jinja (FR-NTF-3)."""

    event = models.CharField(max_length=40, primary_key=True)
    enabled = models.BooleanField(default=True)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["event"]

    def __str__(self) -> str:
        return self.event


class EmailLog(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Not sent"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    at = models.DateTimeField(default=timezone.now, db_index=True)
    event = models.CharField(max_length=40)
    to = models.TextField()
    subject = models.CharField(max_length=300)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.QUEUED)
    detail = models.TextField(blank=True)
    entity_type = models.CharField(max_length=64, blank=True)
    entity_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-at"]

    def __str__(self) -> str:
        return f"{self.event} → {self.to} ({self.status})"
