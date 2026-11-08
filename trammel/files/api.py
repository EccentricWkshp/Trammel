from __future__ import annotations

from uuid import UUID

from django.apps import apps
from django.http import Http404
from ninja import File, Form, Router, Schema, Status
from ninja.files import UploadedFile

from trammel.core import access

from . import services

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
    }


@router.get("/{label}/{object_id}", response=list[FileOut])
def list_files(request, label: str, object_id: UUID):
    access.require(request.actor, "files.view")
    return [_out(link) for link in services.attachments_for(_target(request, label, object_id))]


@router.post("/{label}/{object_id}", response={201: FileOut})
def upload(request, label: str, object_id: UUID, file: File[UploadedFile], title: Form[str] = ""):
    access.require(request.actor, "files.upload")
    target = _target(request, label, object_id)
    services.upload(file, name=file.name, title=title, attach_to=target, user=request.actor.user)
    link = services.attachments_for(target).order_by("-created_at").first()
    return Status(201, _out(link))
