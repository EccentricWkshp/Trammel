"""Labor and machine time against work orders (FR-TIME-1..5)."""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from trammel.core.models import BaseModel
from trammel.core.money import MoneyField, RateField


class TimeEntry(BaseModel):
    class Kind(models.TextChoices):
        LABOR = "labor", "Labor"
        MACHINE = "machine", "Machine"

    class Source(models.TextChoices):
        TIMER = "timer", "Timer"
        MANUAL = "manual", "Manual entry"

    work_order = models.ForeignKey(
        "workorders.WorkOrder", on_delete=models.PROTECT, related_name="time_entries"
    )
    operation = models.ForeignKey(
        "workorders.Operation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="time_entries",
    )
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.LABOR)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="time_entries",
    )
    machine = models.ForeignKey(
        "shop.Machine", null=True, blank=True, on_delete=models.PROTECT, related_name="time_entries"
    )
    #: Recorded by the server, so closing a browser tab never loses a timer.
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    minutes = models.DecimalField(max_digits=9, decimal_places=2, default=Decimal("0"))
    billable = models.BooleanField(default=True)
    cost_rate = RateField(default=Decimal("0"))
    bill_rate = RateField(default=Decimal("0"))
    cost = MoneyField(default=Decimal("0"))
    note = models.CharField(max_length=300, blank=True)
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.TIMER)
    #: Set by billing when the entry is pulled into an invoice (FR-TIME-5).
    invoice_line_id = models.UUIDField(null=True, blank=True, editable=False)

    customer_scope_path = "work_order__job__customer_id"

    class Meta:
        ordering = ["-started_at"]
        verbose_name_plural = "time entries"
        constraints = [
            # One running labor timer per person; one running timer per machine (FR-TIME-2).
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(ended_at__isnull=True, kind="labor", deleted_at__isnull=True),
                name="one_running_labor_timer_per_user",
            ),
            models.UniqueConstraint(
                fields=["machine"],
                condition=Q(ended_at__isnull=True, kind="machine", deleted_at__isnull=True),
                name="one_running_timer_per_machine",
            ),
            models.CheckConstraint(
                condition=Q(kind="labor", user__isnull=False)
                | Q(kind="machine", machine__isnull=False),
                name="time_entry_has_a_subject",
            ),
        ]
        indexes = [models.Index(fields=["work_order", "started_at"])]

    def __str__(self) -> str:
        who = self.user if self.kind == self.Kind.LABOR else self.machine
        return f"{who} on {self.work_order.number}: {self.minutes} min"

    @property
    def is_running(self) -> bool:
        return self.ended_at is None

    @property
    def hours(self) -> Decimal:
        return (self.minutes / 60).quantize(Decimal("0.01"))

    @property
    def bill_amount(self) -> Decimal:
        from trammel.core.money import round_money

        return round_money(self.minutes / 60 * self.bill_rate)
