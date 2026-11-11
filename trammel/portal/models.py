"""The customer portal (FR-PRT): who may sign in, and their one-time codes."""

from __future__ import annotations

import hashlib
import hmac

from django.conf import settings
from django.db import models
from django.utils import timezone

from trammel.core.models import uuid7


class Access(models.Model):
    """Whether a customer may use the portal. Nobody may until staff turn it on."""

    customer = models.OneToOneField(
        "customers.Customer",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="portal_access",
    )
    enabled = models.BooleanField(default=False)
    changed_at = models.DateTimeField(default=timezone.now)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    last_sign_in_at = models.DateTimeField(null=True, blank=True)

    customer_scope_path = "customer_id"

    class Meta:
        verbose_name = "portal access"
        verbose_name_plural = "portal access"

    def __str__(self) -> str:
        return f"Portal access for {self.customer}"


class SignInCode(models.Model):
    """A one-time sign-in code emailed to an address (FR-PRT-1, FR-PRT-6).

    Only a keyed hash is kept, so the codes can't be read back from a copy of the
    database. A code works once, expires quickly and dies after a few wrong tries.
    """

    LIFETIME_MINUTES = 10
    MAX_TRIES = 5

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    email = models.CharField(max_length=254, db_index=True)
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    tries = models.PositiveSmallIntegerField(default=0)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Portal code for {self.email}"

    @staticmethod
    def hash(email: str, code: str) -> str:
        key = settings.SECRET_KEY.encode()
        return hmac.new(key, f"portal:{email}:{code}".encode(), hashlib.sha256).hexdigest()


class Attempt(models.Model):
    """A request for a code, or a try at one: kept for rate limiting (FR-PRT-6)."""

    class Kind(models.TextChoices):
        REQUEST = "request", "Code requested"
        VERIFY = "verify", "Code entered"

    id = models.BigAutoField(primary_key=True)
    at = models.DateTimeField(default=timezone.now, db_index=True)
    kind = models.CharField(max_length=8, choices=Kind.choices)
    email = models.CharField(max_length=254)
    ip = models.GenericIPAddressField(null=True, blank=True)
    #: What the limits count: the address, or an IPv6 address's /64 network.
    ip_key = models.CharField(max_length=64, blank=True)
    succeeded = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["kind", "email", "at"]),
            models.Index(fields=["kind", "ip_key", "at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.email}"
