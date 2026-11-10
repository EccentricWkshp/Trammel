from __future__ import annotations

from uuid import UUID

from django.apps import apps
from django.http import Http404
from ninja import File, Form, Router, Schema, Status
from ninja.files import UploadedFile

from trammel.core import access

from . import services
from .models import DocumentType

router = Router(tags=["files"])


class FileOut(Schema):
    document: UUID
    title: str
    revision: int
    revision_id: UUID
    original_name: str
    type: str
    size: int
    sha256: str
    shared_with_customer: bool
    doc_type: UUID | None
    doc_type_name: str


class DocumentTypeOut(Schema):
    id: UUID
    name: str
    description: str
    extensions: list[str]
    show_on_traveler: bool
    is_active: bool


def _target(request, label: str, object_id: UUID):
    try:
        model = apps.get_model(label)
    except LookupError, ValueError:
        raise Http404 from None
    perm = services.target_permission(model())
    if perm is None:
        raise Http404
    return access.get_or_404(request.actor, perm, model.objects.all(), pk=object_id)


def _out(link) -> dict:
    rev = link.document.current
    return {
        "document": link.document_id,
        "title": link.document.title,
        "revision": rev.number,
        "revision_id": rev.pk,
        "original_name": rev.original_name,
        "type": rev.blob.mime,
        "size": rev.blob.size,
        "sha256": rev.blob.sha256,
        "shared_with_customer": link.shared_with_customer,
        "doc_type": link.document.doc_type_id,
        "doc_type_name": link.document.doc_type.name if link.document.doc_type else "",
    }


@router.get("/types", response=list[DocumentTypeOut])
def list_types(request):
    """Document types, for the `doc_type` of an upload."""
    access.require(request.actor, "files.view")
    return [
        {
            "id": t.pk,
            "name": t.name,
            "description": t.description,
            "extensions": t.extension_list(),
            "show_on_traveler": t.show_on_traveler,
            "is_active": t.is_active,
        }
        for t in DocumentType.objects.all()
    ]


@router.get("/{label}/{object_id}", response=list[FileOut])
def list_files(request, label: str, object_id: UUID):
    access.require(request.actor, "files.view")
    return [_out(link) for link in services.attachments_for(_target(request, label, object_id))]


@router.post("/{label}/{object_id}", response={201: FileOut})
def upload(
    request,
    label: str,
    object_id: UUID,
    file: File[UploadedFile],
    title: Form[str] = "",
    doc_type: Form[UUID | None] = None,
):
    """Attach a file. Without `doc_type`, the type is suggested from the file name."""
    access.require(request.actor, "files.upload")
    target = _target(request, label, object_id)
    chosen = None
    if doc_type is not None:
        chosen = DocumentType.objects.filter(pk=doc_type, is_active=True).first()
        if chosen is None:
            raise Http404
    services.upload(
        file,
        name=file.name,
        title=title,
        attach_to=target,
        doc_type=chosen,
        user=request.actor.user,
    )
    link = services.attachments_for(target).order_by("-created_at").first()
    return Status(201, _out(link))
