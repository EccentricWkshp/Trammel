"""Files: drawings, photos, DXF, LightBurn, STL/3MF, G-code (FR-DOC-1..8).

* `Blob` — stored bytes, one row per distinct SHA-256 (deduplication).
* `Document` — a logical file with a revision history (FR-DOC-3).
* `DocumentRevision` — one uploaded version; append-only.
* `Attachment` — links a document to any record; detaching is not deleting.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone

from trammel.core.models import AppendOnlyModel, BaseModel, uuid7


class Blob(models.Model):
    class Thumb(models.TextChoices):
        NONE = "none", "Not applicable"
        PENDING = "pending", "Pending"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    sha256 = models.CharField(max_length=64, unique=True)
    size = models.BigIntegerField()
    mime = models.CharField(max_length=100)
    kind = models.CharField(max_length=16)
    path = models.CharField(max_length=200)
    thumbnail = models.CharField(max_length=200, blank=True)
    thumb_state = models.CharField(max_length=8, choices=Thumb.choices, default=Thumb.NONE)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return self.sha256[:12]

    @property
    def is_previewable_inline(self) -> bool:
        return self.kind in ("image", "pdf", "svg")


class Document(BaseModel):
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    current = models.ForeignKey(
        "DocumentRevision", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["title"]

    def __str__(self) -> str:
        return self.title

    @property
    def revision_count(self) -> int:
        return self.revisions.count()


class DocumentRevision(AppendOnlyModel):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="revisions")
    number = models.PositiveIntegerField()
    blob = models.ForeignKey(Blob, on_delete=models.PROTECT, related_name="revisions")
    original_name = models.CharField(max_length=255)
    note = models.CharField(max_length=300, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["document", "number"], name="unique_revision_number")
        ]

    def __str__(self) -> str:
        return f"{self.document} r{self.number}"


class Attachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="attachments")
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField()
    target = GenericForeignKey("content_type", "object_id")
    #: Visible to the customer in the portal (FR-PRT-4). Nothing is by default.
    shared_with_customer = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "content_type", "object_id"], name="unique_attachment"
            )
        ]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self) -> str:
        return f"{self.document} → {self.content_type.model} {self.object_id}"
