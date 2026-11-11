"""Customer agreements: the terms accepted before a job starts (FR-AGR-1..7)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from trammel.core.models import AppendOnlyModel, BaseModel, SingletonModel


class AgreementTemplate(BaseModel):
    name = models.CharField(max_length=100, unique=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-is_default", "name"]

    def __str__(self) -> str:
        return self.name

    @property
    def current(self) -> AgreementTemplateVersion | None:
        return self.versions.order_by("-version").first()

    @classmethod
    def default(cls) -> AgreementTemplate | None:
        return cls.objects.filter(is_active=True).order_by("-is_default", "name").first()


class AgreementTemplateVersion(AppendOnlyModel):
    """Editing a template creates a new version (FR-AGR-2). Never changed."""

    template = models.ForeignKey(
        AgreementTemplate, on_delete=models.PROTECT, related_name="versions"
    )
    version = models.PositiveIntegerField()
    body = models.TextField(help_text="The terms and conditions text.")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["template", "version"], name="unique_template_version")
        ]

    def __str__(self) -> str:
        return f"{self.template} v{self.version}"


class CustomerAgreement(BaseModel):
    class Status(models.TextChoices):
        ISSUED = "issued", "Issued"
        ACCEPTED = "accepted", "Accepted"
        SUPERSEDED = "superseded", "Superseded"

    class Method(models.TextChoices):
        MANUAL = "manual", "Recorded by staff"
        UPLOAD = "upload", "Signed document uploaded"
        CLICK = "click", "Accepted in the portal"
        SIGNATURE = "signature", "Signature captured"

    class How(models.TextChoices):
        IN_PERSON = "in_person", "In person"
        PHONE = "phone", "By phone"
        EMAIL = "email", "By email"
        OTHER = "other", "Other"

    job = models.ForeignKey("jobs.Job", on_delete=models.PROTECT, related_name="agreements")
    #: False for amended terms carried by a change order (FR-AGR-6), which the
    #: change order links to; only the job's main agreement gates the start.
    is_primary = models.BooleanField(default=True)
    template_version = models.ForeignKey(
        AgreementTemplateVersion, on_delete=models.PROTECT, related_name="agreements"
    )
    #: The exact text issued — never re-rendered (FR-AGR-3).
    text = models.TextField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ISSUED)
    issued_at = models.DateTimeField(default=timezone.now)

    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by_name = models.CharField(max_length=150, blank=True)
    method = models.CharField(max_length=12, choices=Method.choices, blank=True)
    how = models.CharField(max_length=12, choices=How.choices, blank=True)
    evidence = models.ForeignKey(
        "files.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    evidence_ip = models.GenericIPAddressField(null=True, blank=True)
    #: The drawn signature, as a cropped PNG, when accepted by signing.
    signature = models.BinaryField(blank=True, default=b"", editable=False)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    customer_scope_path = "job__customer_id"

    class Meta:
        ordering = ["-issued_at"]

    def __str__(self) -> str:
        return f"Agreement for {self.job.number} ({self.template_version})"

    def get_absolute_url(self) -> str:
        return reverse("agreements:detail", args=[self.pk])


class AgreementWaiver(BaseModel):
    """A per-job override: this job may start without an agreement (FR-AGR-4)."""

    job = models.OneToOneField(
        "jobs.Job", on_delete=models.PROTECT, related_name="agreement_waiver"
    )
    reason = models.TextField()

    def __str__(self) -> str:
        return f"Waiver for {self.job.number}"


class AgreementSettings(SingletonModel):
    required = models.BooleanField(
        "require an accepted agreement before work starts",
        default=True,
        help_text="Can be waived per job, with a reason (e.g. a small counter job).",
    )
    manual_enabled = models.BooleanField("acceptance recorded by staff", default=True)
    upload_enabled = models.BooleanField("acceptance by uploaded signed document", default=True)
    click_enabled = models.BooleanField(
        "click-to-accept in the customer portal",
        default=True,
        help_text="The customer types their name and ticks a box to accept, when they approve a "
        "quote in the portal. The time and their IP address are recorded.",
    )
    signature_enabled = models.BooleanField(
        "signature capture",
        default=True,
        help_text="The customer draws their signature to accept: in the portal (as well as typing "
        "their name), and on a tablet or phone in the shop with Sign on this device.",
    )

    class Meta:
        verbose_name = "agreement settings"

    def __str__(self) -> str:
        return "Agreement settings"
