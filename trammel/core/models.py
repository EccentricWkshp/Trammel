"""
Core model primitives and core tables.

Primitives (BaseModel, AppendOnlyModel, soft-delete managers) are adapted from
HomeAutoShop's `homeautoshop/core/models.py` (AGPL-3.0, same owner); see NOTICE.

Everything here is enforced in one place rather than remembered at call sites:

* UUIDv7 primary keys (SPEC §4.4).
* Soft delete with a 30-day trash for ordinary records (SPEC §4.4, FR-ADM-9).
* Append-only records that refuse in-place edits (SPEC §3.2).
* Singleton settings rows (FR-ADM-2..4).
"""

from __future__ import annotations

import uuid
import zoneinfo
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from .money import MoneyField, PercentField, RateField

TRASH_RETENTION_DAYS = 30


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562): index locality without a sequence."""
    return uuid.uuid7()


# ---------------------------------------------------------------------------
# Soft delete
# ---------------------------------------------------------------------------


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def in_trash(self):
        cutoff = timezone.now() - timedelta(days=TRASH_RETENTION_DAYS)
        return self.filter(deleted_at__isnull=False, deleted_at__gte=cutoff)

    def delete(self):  # type: ignore[override]
        """Soft-delete the queryset and the children named in `soft_delete_cascade`."""
        stamp = timezone.now()
        pks = list(self.values_list("pk", flat=True))
        for name in getattr(self.model, "soft_delete_cascade", ()):
            field = getattr(self.model, name).rel.field
            field.model.all_objects.filter(
                **{f"{field.name}__in": pks}, deleted_at__isnull=True
            ).update(deleted_at=stamp)
        count = self.model.all_objects.filter(pk__in=pks).update(deleted_at=stamp)
        return count, {self.model._meta.label: count}

    def hard_delete(self):
        return super().delete()


class AliveManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Default manager: hides soft-deleted rows."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class AllObjectsManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Every row including trash — for restore, export and audit."""


class BaseModel(models.Model):
    """Identity, provenance and soft delete: the root of every ordinary record."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        editable=False,
    )
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)

    #: Reverse relations a soft delete follows. A soft delete issues no SQL
    #: DELETE, so `on_delete=CASCADE` never fires; children must be named here
    #: or they stay alive under a parent every screen hides.
    soft_delete_cascade: tuple[str, ...] = ()

    #: Lookup path from this model to the owning customer's id, used by the
    #: record-level permission layer to confine a customer principal to its
    #: own records (SPEC §2, FR-PRT-2). None means customers never see it.
    customer_scope_path: str | None = None

    objects = AliveManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True
        get_latest_by = "created_at"

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def delete(self, using=None, keep_parents=False, hard: bool = False):
        """Soft delete by default."""
        if hard:
            return super().delete(using=using, keep_parents=keep_parents)
        self.deleted_at = timezone.now()
        # One shared timestamp lets restore() tell the children this delete
        # took down from ones somebody had already deleted on their own.
        for name in self.soft_delete_cascade:
            getattr(self, name)(manager="all_objects").filter(deleted_at__isnull=True).update(
                deleted_at=self.deleted_at
            )
        self.save(update_fields=["deleted_at", "updated_at"])
        return (0, {})

    def restore(self) -> None:
        stamp = self.deleted_at
        self.deleted_at = None
        self.save(update_fields=["deleted_at", "updated_at"])
        if stamp is None:
            return
        for name in self.soft_delete_cascade:
            getattr(self, name)(manager="all_objects").filter(deleted_at=stamp).update(
                deleted_at=None
            )


class AppendOnlyModel(models.Model):
    """A record that is never edited in place (SPEC §3.2).

    Corrections are new rows. `server_writable_fields` names derived columns
    the server may recompute (a thumbnail path, say) without making the record
    itself mutable.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False, db_index=True)

    server_writable_fields: frozenset[str] = frozenset()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            fields = set(kwargs.get("update_fields") or ())
            if not fields or not fields <= set(self.server_writable_fields):
                raise ValidationError(
                    f"{type(self).__name__} is append-only; record a correction instead."
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f"{type(self).__name__} is append-only and cannot be deleted.")


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------


class SingletonModel(models.Model):
    """A table with exactly one row, pk=1."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.pk = 1
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Settings rows cannot be deleted.")

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


def validate_timezone(value: str) -> None:
    if value not in zoneinfo.available_timezones():
        raise ValidationError(f"{value!r} is not a known time zone.")


def validate_currency(value: str) -> None:
    from babel.core import UnknownLocaleError
    from babel.numbers import get_currency_name

    try:
        known = get_currency_name(value, locale="en") != value
    except UnknownLocaleError:  # pragma: no cover - "en" always exists
        known = False
    if not known:
        raise ValidationError(f"{value!r} is not an ISO 4217 currency code.")


class SystemSettings(SingletonModel):
    """System configuration (FR-ADM-2) — infrastructure the admin controls in the UI."""

    setup_completed_at = models.DateTimeField(null=True, blank=True, editable=False)
    offline_mode = models.BooleanField(
        default=False,
        help_text="Turn off all outbound traffic: update check, email, order imports.",
    )
    update_check_enabled = models.BooleanField(
        default=True, help_text="Check GitHub releases for a newer version and tell the admin."
    )
    require_mfa_for_admins = models.BooleanField(
        "require two-factor sign-in for system admins", default=False
    )
    audit_retention_days = models.PositiveIntegerField(
        default=0, help_text="Delete audit entries older than this. 0 keeps them forever."
    )

    class Meta:
        verbose_name = "system settings"
        verbose_name_plural = "system settings"

    def __str__(self) -> str:
        return str(self._meta.verbose_name)

    @property
    def is_offline(self) -> bool:
        return self.offline_mode or settings.OFFLINE_MODE


class BusinessProfile(SingletonModel):
    """Who the business is (FR-ADM-1, FR-ADM-3)."""

    class UnitSystem(models.TextChoices):
        IMPERIAL = "imperial", "Imperial (in, ft, lb)"
        METRIC = "metric", "Metric (mm, m, kg)"

    name = models.CharField(max_length=200, default="My Shop")
    legal_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    website = models.URLField(blank=True)
    address_line1 = models.CharField("address", max_length=200, blank=True)
    address_line2 = models.CharField("address line 2", max_length=200, blank=True)
    city = models.CharField(max_length=100, blank=True)
    region = models.CharField("state / province", max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, blank=True, default="US", help_text="ISO country code")
    tax_registration_numbers = models.TextField(
        blank=True, help_text="One per line, e.g. “Sales tax permit: 123-456-789”."
    )
    default_payment_terms_days = models.PositiveSmallIntegerField(
        default=30, help_text="Days after the invoice date that payment is due."
    )
    timezone = models.CharField(max_length=64, default="UTC", validators=[validate_timezone])

    class Weekday(models.IntegerChoices):
        # Python's numbering (calendar module): Monday is 0.
        MONDAY = 0, "Monday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    date_format = models.CharField(
        "date and time format",
        max_length=4,
        default="us",
        choices=[
            ("us", "11/07/2026 and 2:30 PM (US)"),
            ("iso", "2026-11-07 and 14:30 (ISO)"),
            ("dmy", "07/11/2026 and 14:30 (day first)"),
        ],
    )
    week_start = models.PositiveSmallIntegerField(
        "week starts on",
        choices=Weekday.choices,
        default=Weekday.SUNDAY,
        help_text="For calendars. Sunday is the US convention.",
    )
    unit_system = models.CharField(
        max_length=10, choices=UnitSystem.choices, default=UnitSystem.IMPERIAL
    )
    currency = models.CharField(
        max_length=3,
        default="USD",
        validators=[validate_currency],
        help_text="Locked once any quote or invoice exists.",
    )

    class Meta:
        verbose_name = "business profile"

    def __str__(self) -> str:
        return self.name


class ShopRates(SingletonModel):
    """Business costs that feed quotes and job costing (FR-ADM-4)."""

    labor_hourly_rate = MoneyField(default=Decimal("0"), validators=[MinValueValidator(0)])
    labor_daily_rate = MoneyField(default=Decimal("0"), validators=[MinValueValidator(0)])
    shop_overhead_rate = RateField(
        "overhead / shop rate (per hour)",
        default=Decimal("0"),
        validators=[MinValueValidator(0)],
    )
    default_material_markup = PercentField("default markup on materials (%)", default=Decimal("0"))
    consumables_allowance = PercentField(
        "consumables / supplies allowance (%)", default=Decimal("0")
    )
    minimum_job_charge = MoneyField(default=Decimal("0"), validators=[MinValueValidator(0)])
    electricity_rate = RateField(
        "electricity price (per kWh)",
        default=Decimal("0.15"),
        validators=[MinValueValidator(0)],
        help_text="From your power bill; used by the machine rate calculator.",
    )

    class Meta:
        verbose_name = "shop rates"
        verbose_name_plural = "shop rates"

    def __str__(self) -> str:
        return str(self._meta.verbose_name)


# ---------------------------------------------------------------------------
# Module registry state (SPEC §4.5)
# ---------------------------------------------------------------------------


class ModuleState(models.Model):
    """Whether an installed module is enabled. Installed = present in the code."""

    key = models.CharField(max_length=64, primary_key=True)
    enabled = models.BooleanField(default=True)
    changed_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.key}: {'enabled' if self.enabled else 'disabled'}"


# ---------------------------------------------------------------------------
# Document numbering (SPEC §4.4)
# ---------------------------------------------------------------------------


class DocumentSequence(models.Model):
    """Configuration for one human-readable number series, e.g. `J-2026-0042`."""

    key = models.CharField(max_length=32, primary_key=True)
    label = models.CharField(max_length=64)
    prefix = models.CharField(max_length=12)
    yearly = models.BooleanField(
        default=True, help_text="Include the year and restart numbering each January."
    )
    padding = models.PositiveSmallIntegerField(default=4)

    class Meta:
        ordering = ["label"]

    def __str__(self) -> str:
        return self.label

    def format(self, value: int, year: int) -> str:
        number = f"{value:0{self.padding}d}"
        return f"{self.prefix}-{year}-{number}" if self.yearly else f"{self.prefix}-{number}"


class SequenceCounter(models.Model):
    """The last number issued in a series and period. Locked to allocate."""

    sequence = models.ForeignKey(
        DocumentSequence, on_delete=models.CASCADE, related_name="counters"
    )
    year = models.PositiveSmallIntegerField()  # 0 for a series that does not reset
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["sequence", "year"], name="unique_counter_period")
        ]

    def __str__(self) -> str:
        return f"{self.sequence_id} {self.year}: {self.last_value}"


# ---------------------------------------------------------------------------
# Audit log (FR-AUD) and lifecycle history (SPEC §6)
# ---------------------------------------------------------------------------


class ActorKind(models.TextChoices):
    USER = "user", "User"
    CUSTOMER = "customer", "Customer"
    API_KEY = "api_key", "API key"
    SYSTEM = "system", "System"
    ANONYMOUS = "anonymous", "Anonymous"


class AuditEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("The audit log is append-only.")

    def delete(self):
        raise ValidationError("The audit log is append-only; use AuditEvent.purge_before().")


class AuditEvent(AppendOnlyModel):
    """Who did what, to which record, when (FR-AUD-1). Never edited.

    A database trigger (migration 0002) refuses UPDATE as well, so the rule
    holds even for code that bypasses the ORM.
    """

    at = models.DateTimeField(default=timezone.now, db_index=True)
    actor_kind = models.CharField(max_length=16, choices=ActorKind.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    customer_id = models.UUIDField(null=True, blank=True)
    api_key_id = models.UUIDField(null=True, blank=True)
    actor_label = models.CharField(max_length=200, blank=True)
    action = models.CharField(max_length=48, db_index=True)
    entity_type = models.CharField(max_length=64, blank=True, db_index=True)
    entity_id = models.CharField(max_length=64, blank=True, db_index=True)
    entity_label = models.CharField(max_length=200, blank=True)
    summary = models.CharField(max_length=300, blank=True)
    reason = models.TextField(blank=True)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    source = models.CharField(max_length=16, default="web")

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["entity_type", "entity_id", "-at"])]

    def __str__(self) -> str:
        return f"{self.at:%Y-%m-%d %H:%M} {self.actor_label} {self.action} {self.entity_label}"

    def changes(self) -> list[tuple[str, object, object]]:
        """(field, before, after) for display, in a stable order."""
        keys = sorted(set(self.before) | set(self.after))
        return [(k, self.before.get(k), self.after.get(k)) for k in keys]

    @classmethod
    def purge_before(cls, cutoff) -> int:
        """Retention purge (FR-AUD-3) — the only way rows ever leave."""
        qs = models.QuerySet(cls).filter(at__lt=cutoff)
        return qs._raw_delete(qs.db)


class StateChange(AppendOnlyModel):
    """One lifecycle transition, for record timelines and reports (SPEC §6.2)."""

    class Kind(models.TextChoices):
        FORWARD = "forward", "Forward"
        REOPEN = "reopen", "Reopened"
        FORCE = "force", "Forced"

    at = models.DateTimeField(default=timezone.now)
    entity_type = models.CharField(max_length=64)
    entity_id = models.UUIDField()
    field = models.CharField(max_length=32, default="status")
    from_state = models.CharField(max_length=32)
    to_state = models.CharField(max_length=32)
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.FORWARD)
    actor_kind = models.CharField(max_length=16, choices=ActorKind.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["at"]
        indexes = [models.Index(fields=["entity_type", "entity_id", "at"])]

    def __str__(self) -> str:
        return f"{self.entity_type} {self.from_state} → {self.to_state}"


# ---------------------------------------------------------------------------
# Background tasks (SPEC §3.5) and stored credentials (SPEC §9)
# ---------------------------------------------------------------------------


class BackgroundTask(models.Model):
    """One unit of queued work. See core/tasks.py."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=8, choices=State.choices, default=State.PENDING)
    run_after = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    last_error = models.TextField(blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["state", "run_after"])]

    def __str__(self) -> str:
        return f"{self.name} [{self.state}]"


class PeriodicRun(models.Model):
    name = models.CharField(max_length=64, primary_key=True)
    last_enqueued_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return self.name


class Credential(models.Model):
    """A secret (SMTP password, importer token), encrypted at rest (SPEC §9).

    Never rendered back: a credential can be replaced or cleared, not read in
    the UI. Kept apart from settings so exports can leave the table out.
    """

    key = models.CharField(max_length=64, primary_key=True)
    ciphertext = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.key
