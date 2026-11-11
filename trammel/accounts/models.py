"""
Users, roles, sign-in throttling and API keys (SPEC §2, FR-ADM-5, FR-ADM-6, FR-API-2).

Only the system admin is built in (`User.is_admin`). Every other role is defined
by the admin from module permission codenames. Users are deactivated, never
deleted, so nothing they authored is lost or reassigned (FR-ADM-5).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.utils.timezone import now

from trammel.core import access, modules
from trammel.core.models import uuid7, validate_timezone
from trammel.core.money import RateField


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email: str, password: str | None = None, **extra):
        if not email:
            raise ValueError("An email address is required.")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_admin(self, email: str, password: str, **extra):
        return self.create_user(email, password, is_admin=True, **extra)

    def get_by_natural_key(self, username):
        return self.get(email__iexact=username)


class Role(models.Model):
    """A user-defined role: a named set of permission codenames (FR-ADM-6)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=80, unique=True)
    description = models.CharField(max_length=300, blank=True)
    permissions = ArrayField(models.CharField(max_length=80), default=list, blank=True)
    require_mfa = models.BooleanField(
        "require two-factor sign-in",
        default=False,
        help_text="Users with this role must set up an authenticator app.",
    )
    created_at = models.DateTimeField(default=now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        known = modules.all_permissions()
        unknown = sorted(set(self.permissions) - set(known))
        if unknown:
            raise ValidationError({"permissions": f"Unknown permissions: {', '.join(unknown)}"})
        self.permissions = sorted(set(self.permissions))


class User(AbstractBaseUser):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    #: The built-in system admin role (SPEC §2): every permission, plus users,
    #: roles, modules, integrations, backups and system settings.
    is_admin = models.BooleanField("system admin", default=False)
    roles = models.ManyToManyField(Role, blank=True, related_name="users")
    timezone = models.CharField(
        max_length=64,
        blank=True,
        validators=[validate_timezone],
        help_text="Leave blank to use the business time zone.",
    )
    labor_cost_rate = RateField(
        "labor cost rate (per hour)",
        null=True,
        blank=True,
        help_text="What an hour of this person's time costs the shop. "
        "Blank uses the shop's labor cost per hour.",
    )
    # Second factor (SPEC §9). The secret is encrypted; recovery codes are hashed.
    totp_secret = models.TextField(blank=True, editable=False)
    totp_confirmed_at = models.DateTimeField(null=True, blank=True, editable=False)
    totp_last_step = models.BigIntegerField(default=0, editable=False)
    recovery_codes = models.JSONField(default=list, blank=True, editable=False)
    #: The home screen as this user arranged it: [{"key": ..., "on": bool}, ...]
    #: in display order (core/dashboard.py). Empty means the defaults.
    home_sections = models.JSONField(default=list, blank=True, editable=False)
    #: Small per-person choices that modules keep under their own key, such as
    #: {"calendar": {"view": "week", "week": "work"}}.
    preferences = models.JSONField(default=dict, blank=True, editable=False)
    date_joined = models.DateTimeField(default=now, editable=False)
    deactivated_at = models.DateTimeField(null=True, blank=True, editable=False)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        ordering = ["full_name"]
        constraints = [
            models.UniqueConstraint(Lower("email"), name="unique_user_email_ci"),
        ]

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        return self.full_name or self.email

    def get_full_name(self) -> str:
        return self.display_name

    def get_short_name(self) -> str:
        return self.full_name.split(" ")[0] if self.full_name else self.email

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    # Django's permission protocol, so `{% if perms.customers.edit %}` in a
    # template goes through the same layer as everything else.
    def has_perm(self, perm: str, obj=None) -> bool:
        return access.has_perm(self, perm, obj)

    def has_perms(self, perm_list, obj=None) -> bool:
        return all(self.has_perm(p, obj) for p in perm_list)

    def has_module_perms(self, app_label: str) -> bool:
        return any(
            c.startswith(f"{app_label}.") for c in access.permissions_of(access.as_actor(self))
        )


class LoginAttempt(models.Model):
    """One sign-in attempt, kept for rate limiting and lockout (SPEC §9)."""

    id = models.BigAutoField(primary_key=True)
    at = models.DateTimeField(default=now, db_index=True)
    email = models.CharField(max_length=254, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    succeeded = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["ip", "at"])]

    def __str__(self) -> str:
        return f"{self.email} {'ok' if self.succeeded else 'failed'} {self.at:%Y-%m-%d %H:%M}"


class ApiKeyQuerySet(models.QuerySet):
    def usable(self):
        return self.filter(revoked_at__isnull=True, user__is_active=True).filter(
            models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now())
        )


class ApiKey(models.Model):
    """A personal API key (FR-API-2).

    The token is shown once, at creation. Only its SHA-256 is stored: the token
    carries 256 bits of randomness, so a slow password hash would add cost and
    no safety. The scope can never exceed the owner's own permissions, because
    the effective permission set is the intersection, computed at use.
    """

    TOKEN_PREFIX = "trm"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="api_keys")
    name = models.CharField(max_length=80)
    lookup = models.CharField(max_length=16, unique=True, editable=False)
    secret_hash = models.CharField(max_length=64, editable=False)
    scopes = ArrayField(models.CharField(max_length=80), default=list, blank=True)
    created_at = models.DateTimeField(default=now, editable=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True, editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True, editable=False)

    objects = ApiKeyQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.TOKEN_PREFIX}_{self.lookup}…)"

    @staticmethod
    def _hash(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()

    @classmethod
    def issue(cls, user: User, name: str, scopes: list[str], expires_in_days: int | None = None):
        """Create a key. Returns (key, token); the token is never retrievable again."""
        held = access.permissions_of(access.as_actor(user))
        excess = sorted(set(scopes) - held)
        if excess:
            raise ValidationError(f"You do not hold: {', '.join(excess)}")
        lookup = secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        key = cls.objects.create(
            user=user,
            name=name,
            lookup=lookup,
            secret_hash=cls._hash(secret),
            scopes=sorted(set(scopes)),
            expires_at=now() + timedelta(days=expires_in_days) if expires_in_days else None,
        )
        return key, f"{cls.TOKEN_PREFIX}_{lookup}_{secret}"

    @classmethod
    def authenticate(cls, token: str) -> ApiKey | None:
        try:
            prefix, lookup, secret = token.split("_", 2)
        except ValueError:
            return None
        if prefix != cls.TOKEN_PREFIX:
            return None
        key = cls.objects.usable().select_related("user").filter(lookup=lookup).first()
        if key is None or not secrets.compare_digest(key.secret_hash, cls._hash(secret)):
            return None
        return key

    @property
    def is_usable(self) -> bool:
        return (
            self.revoked_at is None
            and self.user.is_active
            and (self.expires_at is None or self.expires_at > now())
        )

    def actor(self) -> access.Actor:
        return access.Actor(
            kind="api_key",
            user=self.user,
            api_key_id=self.id,
            scope=frozenset(self.scopes),
            label=f"{self.user.display_name} (API key “{self.name}”)",
        )


class MagicLink(models.Model):
    """A single-use emailed sign-in link (SPEC §9). One factor, not MFA."""

    LIFETIME_MINUTES = 15

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="magic_links")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=now)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Sign-in link for {self.user}"

    @staticmethod
    def hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @classmethod
    def issue(cls, user, ip=None) -> str:
        token = secrets.token_urlsafe(32)
        cls.objects.create(
            user=user,
            token_hash=cls.hash(token),
            ip=ip,
            expires_at=now() + timedelta(minutes=cls.LIFETIME_MINUTES),
        )
        return token

    @classmethod
    def redeem(cls, token: str):
        """The user for a valid, unused link — which is then used up. Else None."""
        link = (
            cls.objects.select_related("user")
            .filter(
                token_hash=cls.hash(token),
                used_at__isnull=True,
                expires_at__gt=now(),
                user__is_active=True,
            )
            .first()
        )
        if link is None:
            return None
        updated = cls.objects.filter(pk=link.pk, used_at__isnull=True).update(used_at=now())
        return link.user if updated else None
