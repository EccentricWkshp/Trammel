from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction

from trammel.core import audit, tasks

from . import sniff
from .models import Attachment, Blob, Document, DocumentRevision, DocumentType, ShopDocument

_SNIFF_BYTES = 64 * 1024


def target_permission(obj: models.Model) -> str | None:
    """The view permission for a record files may attach to; None if they may not.

    Modules declare attachable records in their manifest (`attachable`).
    """
    from trammel.core.modules import manifests

    label = obj._meta.label_lower
    for manifest in manifests():
        for model_label, perm in manifest.attachable:
            if model_label.lower() == label:
                return perm
    return None


def media_root() -> Path:
    return Path(settings.MEDIA_ROOT)


def _store(data_path: Path, name: str) -> Blob:
    """Hash, sniff and store a temp file; reuse an identical blob if one exists."""
    size = data_path.stat().st_size
    with data_path.open("rb") as fh:
        head = fh.read(_SNIFF_BYTES)
    detected = sniff.detect(head, name, size, path=data_path)

    if detected.mime == "image/jpeg" and settings.STRIP_GPS_EXIF:
        original = data_path.read_bytes()
        stripped = sniff.strip_jpeg_gps(original)
        if stripped is not original:
            data_path.write_bytes(stripped)
            size = len(stripped)

    digest = hashlib.sha256()
    with data_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    sha = digest.hexdigest()

    existing = Blob.objects.filter(sha256=sha).first()
    if existing is not None:
        data_path.unlink(missing_ok=True)
        return existing

    relative = Path("blobs") / sha[:2] / sha[2:4] / sha
    final = media_root() / relative
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(data_path, final)
    thumb = Blob.Thumb.PENDING if detected.kind in ("image", "pdf") else Blob.Thumb.NONE
    try:
        with transaction.atomic():
            blob = Blob.objects.create(
                sha256=sha,
                size=size,
                mime=detected.mime,
                kind=detected.kind,
                path=str(relative).replace("\\", "/"),
                thumb_state=thumb,
            )
    except IntegrityError:  # stored concurrently by someone else
        return Blob.objects.get(sha256=sha)
    if thumb == Blob.Thumb.PENDING:
        transaction.on_commit(lambda: tasks.enqueue("files.thumbnail", {"blob": str(blob.pk)}))
    return blob


def _spool(upload) -> Path:
    """Copy an upload to a temp file, enforcing the size limit while reading."""
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    tmp_dir = media_root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=tmp_dir)
    written = 0
    with os.fdopen(fd, "wb") as out:
        chunks = (
            upload.chunks()
            if hasattr(upload, "chunks")
            else iter(lambda: upload.read(1 << 20), b"")
        )
        for chunk in chunks:
            written += len(chunk)
            if written > limit:
                out.close()
                Path(name).unlink(missing_ok=True)
                raise ValidationError(f"Files are limited to {settings.MAX_UPLOAD_MB} MB.")
            out.write(chunk)
    return Path(name)


@transaction.atomic
def upload(
    upload,
    *,
    name: str,
    title: str = "",
    document: Document | None = None,
    attach_to: models.Model | None = None,
    note: str = "",
    doc_type: DocumentType | None = None,
    user=None,
) -> DocumentRevision:
    """Store an upload as a new document, or as the next revision of `document`.

    A new document without a type gets the one its file name suggests.
    """
    blob = _store(_spool(upload), name)
    creating = document is None
    if creating:
        document = Document.objects.create(
            title=title or name,
            doc_type=doc_type or DocumentType.suggest(name),
            created_by=user,
        )
        number = 1
    else:
        number = (document.revisions.aggregate(models.Max("number"))["number__max"] or 0) + 1
    revision = DocumentRevision.objects.create(
        document=document,
        number=number,
        blob=blob,
        original_name=name[:255],
        note=note,
        uploaded_by=user,
    )
    document.current = revision
    document.save(update_fields=["current", "updated_at"])
    audit.record(
        "file.upload",
        document,
        summary=f"Uploaded {name}" + (f" (revision {number})" if not creating else ""),
        after={"sha256": blob.sha256, "size": blob.size, "type": blob.mime, "revision": number},
    )
    if attach_to is not None:
        attach(document, attach_to, user=user)
    return revision


@transaction.atomic
def update_document(document: Document, *, title: str, description: str, doc_type) -> Document:
    from trammel.core.services import save_audited

    before = audit.snapshot(document)
    document.title, document.description, document.doc_type = title, description, doc_type
    return save_audited(document, before=before)


@transaction.atomic
def save_type(doc_type: DocumentType, *, before: dict | None = None) -> DocumentType:
    from django.utils.text import slugify

    from trammel.core.services import save_audited

    if not doc_type.slug:
        base = slugify(doc_type.name)[:34] or "type"
        slug, n = base, 2
        while DocumentType.objects.filter(slug=slug).exists():
            slug, n = f"{base}-{n}", n + 1
        doc_type.slug = slug
    return save_audited(doc_type, before=before)


def attach(document: Document, target: models.Model, *, user=None) -> Attachment:
    if target_permission(target) is None:
        raise ValidationError(f"Files cannot be attached to {target._meta.verbose_name}.")
    ct = ContentType.objects.get_for_model(target)
    link, created = Attachment.objects.get_or_create(
        document=document, content_type=ct, object_id=target.pk, defaults={"created_by": user}
    )
    if created:
        audit.record("file.attach", target, summary=f"Attached {document} to {target}")
    return link


def detach(link: Attachment) -> None:
    target_label = f"{link.content_type.model} {link.object_id}"
    audit.record(
        "file.detach", link.document, summary=f"Detached {link.document} from {target_label}"
    )
    link.delete()


def attachments_for(obj: models.Model):
    ct = ContentType.objects.get_for_model(obj)
    return (
        Attachment.objects.filter(content_type=ct, object_id=obj.pk, document__deleted_at=None)
        .select_related("document__current__blob", "document__doc_type")
        .order_by(
            models.F("document__doc_type__position").asc(nulls_last=True),
            "document__doc_type__name",
            "created_at",
        )
    )


# ---------------------------------------------------------------------------
# Certificates: a customer's tax exemption, the shop's resale certificate
# ---------------------------------------------------------------------------

#: The document type that marks a file as a tax exemption certificate.
EXEMPTION = "exemption"


def exemption_type() -> DocumentType | None:
    return DocumentType.objects.filter(slug=EXEMPTION).first()


def certificate_for(obj: models.Model) -> Attachment | None:
    """The newest tax exemption certificate attached to `obj` (a customer)."""
    return (
        attachments_for(obj)
        .filter(document__doc_type__slug=EXEMPTION)
        .order_by("-document__current__created_at")
        .first()
    )


@transaction.atomic
def mark_certificate(link: Attachment) -> Document:
    """Mark an attached file as the record's tax exemption certificate."""
    doc_type = exemption_type()
    if doc_type is None:
        raise ValidationError("The Tax exemption certificate document type has been deleted.")
    document = link.document
    return update_document(
        document, title=document.title, description=document.description, doc_type=doc_type
    )


def shop_document(key: str) -> Document | None:
    slot = ShopDocument.objects.filter(key=key, document__deleted_at=None).first()
    return slot.document if slot else None


SHOP_DOCUMENT_TITLES = {ShopDocument.RESALE_CERTIFICATE: "Resale certificate"}


@transaction.atomic
def keep_shop_document(key: str, file, *, user=None) -> DocumentRevision:
    """Store a new copy of one of the shop's own documents (its resale certificate)."""
    if key not in SHOP_DOCUMENT_TITLES:
        raise ValidationError(f"There is no shop document called {key!r}.")
    current = shop_document(key)
    if current is not None:
        return upload(file, name=file.name, document=current, user=user)
    revision = upload(
        file,
        name=file.name,
        title=SHOP_DOCUMENT_TITLES[key],
        doc_type=exemption_type(),
        user=user,
    )
    ShopDocument.objects.update_or_create(key=key, defaults={"document": revision.document})
    return revision


def blob_path(blob: Blob) -> Path:
    return media_root() / blob.path
