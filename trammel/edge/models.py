"""The edge portal's settings and what has been published to it (SPEC §12, 0.9.x)."""

from __future__ import annotations

from django.db import models

from trammel.core.models import SingletonModel


class EdgeSettings(SingletonModel):
    """Where the edge portal is on Cloudflare, and how its sync is doing. The API
    token and the sync key are kept encrypted (core.secrets), not here."""

    account_id = models.CharField(
        "Cloudflare account",
        max_length=32,
        blank=True,
        help_text="Filled in when the token can reach only one account.",
    )
    account_name = models.CharField(max_length=200, blank=True, editable=False)
    hostname = models.CharField(
        "custom address",
        max_length=253,
        blank=True,
        help_text="Such as portal.example.com, on a domain whose DNS is on Cloudflare. "
        "Leave it blank for the account's free workers.dev address.",
    )
    worker_name = models.CharField(max_length=63, default="trammel-portal", editable=False)
    database_id = models.CharField(max_length=64, blank=True, editable=False)
    bucket = models.CharField(max_length=63, blank=True, editable=False)
    public_url = models.URLField(blank=True, editable=False)
    deployed_version = models.CharField(max_length=40, blank=True, editable=False)
    deployed_at = models.DateTimeField(null=True, blank=True, editable=False)
    #: Deploying, as a background task: its steps so far, and whether it's done.
    deploying = models.BooleanField(default=False, editable=False)
    deploy_log = models.JSONField(default=list, editable=False)
    #: Serving customers: Trammel's own portal pages are off while this is on.
    active = models.BooleanField(default=False, editable=False)
    #: The last inbox event applied.
    inbox_after = models.BigIntegerField(default=0, editable=False)
    last_sync_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.TextField(blank=True, editable=False)
    last_error_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "edge portal settings"

    def __str__(self) -> str:
        return "Edge portal settings"


class Published(models.Model):
    """A record on the edge, and the files stored with it, so a change is pushed
    once and something no longer shown is taken down."""

    type = models.CharField(max_length=20)
    key = models.CharField(max_length=100)
    fingerprint = models.CharField(max_length=64)
    objects_stored = models.JSONField(default=list)
    pushed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["type", "key"], name="edge_published_unique")
        ]

    def __str__(self) -> str:
        return f"{self.type} {self.key}"


class EdgeAccessCode(models.Model):
    """A code staff made for a customer (FR-PRT-10), hashed with the edge's sync
    key so the Worker can check it. Made with the code, the one time it's known."""

    code = models.OneToOneField("portal.AccessCode", on_delete=models.CASCADE, related_name="edge")
    code_hash = models.CharField(max_length=64)

    def __str__(self) -> str:
        return f"Edge copy of {self.code}"
