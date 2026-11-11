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


class AccessCode(models.Model):
    """A one-time sign-in code staff make and give to the customer themselves, in
    person or by phone, instead of emailing one (FR-PRT-10).

    Not tied to an address, so it is long enough that it can't be guessed: 12
    characters, about 60 bits. Only a keyed hash is kept, and it is shown to staff
    once. It works once, expires in days, and a customer has one at a time.
    """

    LIFETIME_DAYS = 7
    LENGTH = 12
    #: Crockford's base 32: no I, L, O or U, so it reads aloud and copies cleanly.
    ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.CASCADE, related_name="portal_codes"
    )
    code_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)

    customer_scope_path = "customer_id"

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Portal sign-in code for {self.customer}"

    @classmethod
    def normalize(cls, code: str) -> str:
        """As typed: any case, spaces or dashes, and O, I or L for 0, 1 and 1."""
        code = (code or "").upper().translate(str.maketrans("OIL", "011"))
        return "".join(ch for ch in code if ch in cls.ALPHABET)[: cls.LENGTH * 2]

    @staticmethod
    def hash(code: str) -> str:
        key = settings.SECRET_KEY.encode()
        return hmac.new(key, f"portal-access:{code}".encode(), hashlib.sha256).hexdigest()

    @property
    def is_live(self) -> bool:
        return (
            self.used_at is None and self.canceled_at is None and self.expires_at > timezone.now()
        )


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
