from __future__ import annotations

from django import forms
from django.apps import apps
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from trammel.core import access, audit
from trammel.core.web import permission_required

from . import services
from .models import Attachment, Document, DocumentRevision, DocumentType


def _back(request, fallback="/"):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return fallback


def _target(request, label: str, object_id):
    try:
        model = apps.get_model(label)
    except LookupError, ValueError:
        raise Http404 from None
    perm = services.target_permission(model())
    if perm is None:
        raise Http404
    return access.get_or_404(request.actor, perm, model.objects.all(), pk=object_id)


def _may_see(request, document: Document) -> bool:
    """A document is visible through any record it is attached to (FR-DOC-8)."""
    if not access.has_perm(request.actor, "files.view"):
        return False
    if not request.actor.is_customer:
        return True
    for link in document.attachments.filter(shared_with_customer=True):
        target = link.target
        perm = services.target_permission(target) if target is not None else None
        if perm and access.has_perm(request.actor, perm, target):
            return True
    return False


@permission_required("files.upload")
@require_POST
def attach_upload(request, label, object_id):
    target = _target(request, label, object_id)
    upload = request.FILES.get("file")
    if not upload:
        messages.error(request, "Choose a file to upload.")
    else:
        try:
            services.upload(
                upload,
                name=upload.name,
                title=request.POST.get("title", ""),
                attach_to=target,
                doc_type=_chosen_type(request.POST.get("doc_type")),
                user=request.user,
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{upload.name} uploaded.")
    return redirect(_back(request, getattr(target, "get_absolute_url", lambda: "/")()))


@permission_required("files.upload")
@require_POST
def new_revision(request, pk):
    document = get_object_or_404(Document, pk=pk)
    if not _may_see(request, document):
        raise Http404
    upload = request.FILES.get("file")
    if not upload:
        messages.error(request, "Choose a file to upload.")
    else:
        try:
            rev = services.upload(
                upload,
                name=upload.name,
                document=document,
                note=request.POST.get("note", ""),
                user=request.user,
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{document} is now at revision {rev.number}.")
    return redirect(_back(request))


@permission_required("files.upload")
@require_POST
def detach(request, pk):
    link = get_object_or_404(Attachment, pk=pk)
    target = link.target
    perm = services.target_permission(target) if target is not None else None
    if perm is None or not access.has_perm(request.actor, perm, target):
        raise PermissionDenied
    services.detach(link)
    messages.success(request, f"{link.document} detached. The file itself is kept.")
    return redirect(_back(request))


@permission_required("files.upload")
@require_POST
def toggle_share(request, pk):
    """Share a file with the customer, or stop. What a customer can download in
    the portal rests on this, so it needs the right to see the record it is
    attached to, as detaching does."""
    link = get_object_or_404(Attachment, pk=pk)
    target = link.target
    perm = services.target_permission(target) if target is not None else None
    if perm is None or not access.has_perm(request.actor, perm, target):
        raise PermissionDenied
    link.shared_with_customer = not link.shared_with_customer
    link.save(update_fields=["shared_with_customer"])
    from trammel.core import audit

    audit.record(
        "file.share" if link.shared_with_customer else "file.unshare",
        link.document,
        summary=f"{'Shared' if link.shared_with_customer else 'Unshared'} {link.document} "
        "with the customer",
    )
    return redirect(_back(request))


def _serve(path, *, mime: str, filename: str, inline: bool):
    if not path.exists():
        raise Http404
    response = FileResponse(
        path.open("rb"), content_type=mime, as_attachment=not inline, filename=filename
    )
    response["X-Content-Type-Options"] = "nosniff"
    # An SVG opened directly must not run script; a sandboxed CSP prevents it.
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
    response["Cache-Control"] = "private, max-age=3600"
    return response


@permission_required("files.view")
def download(request, pk):
    revision = get_object_or_404(DocumentRevision.objects.select_related("blob", "document"), pk=pk)
    if not _may_see(request, revision.document):
        raise Http404
    blob = revision.blob
    inline = request.GET.get("inline") == "1" and blob.is_previewable_inline
    return _serve(
        services.blob_path(blob), mime=blob.mime, filename=revision.original_name, inline=inline
    )


@permission_required("files.view")
def thumbnail(request, pk):
    document = get_object_or_404(Document.objects.select_related("current__blob"), pk=pk)
    if not _may_see(request, document) or document.current is None:
        raise Http404
    blob = document.current.blob
    if blob.kind == "svg":
        return _serve(services.blob_path(blob), mime=blob.mime, filename="preview.svg", inline=True)
    if not blob.thumbnail:
        raise Http404
    return _serve(
        services.media_root() / blob.thumbnail,
        mime="image/webp",
        filename="thumbnail.webp",
        inline=True,
    )


def _chosen_type(value) -> DocumentType | None:
    """A type picked on an upload form; blank means suggest one from the file name."""
    if not value:
        return None
    try:
        return DocumentType.objects.filter(pk=value, is_active=True).first()
    except ValidationError:
        return None


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------


@permission_required("files.upload")
@require_POST
def use_as_certificate(request):
    """Mark a file already attached to a customer as their exemption certificate."""
    try:
        link = Attachment.objects.select_related("document").get(pk=request.POST.get("link"))
    except Attachment.DoesNotExist, ValidationError:
        raise Http404 from None
    target = link.target
    perm = services.target_permission(target) if target is not None else None
    if perm is None or not access.has_perm(request.actor, perm, target):
        raise Http404
    try:
        services.mark_certificate(link)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"{link.document} is the tax exemption certificate.")
    return redirect(_back(request, getattr(target, "get_absolute_url", lambda: "/")()))


@permission_required("files.upload")
@require_POST
def shop_document_upload(request, key):
    """A new copy of one of the shop's own documents, from business settings."""
    access.require(request.actor, "core.manage_business")
    file = request.FILES.get("file")
    if not file:
        messages.error(request, "Choose a file to upload.")
    else:
        try:
            services.keep_shop_document(key, file, user=request.user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{file.name} uploaded.")
    return redirect(_back(request, "/settings/"))


# ---------------------------------------------------------------------------
# Editing a document's title, description and type
# ---------------------------------------------------------------------------


class DocumentForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ["title", "doc_type", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A type switched off stays selectable on the files that already use it.
        current = self.instance.doc_type_id
        self.fields["doc_type"].queryset = DocumentType.objects.filter(
            Q(is_active=True) | Q(pk=current)
        )
        self.fields["doc_type"].empty_label = "No type"


@permission_required("files.upload")
def document_edit(request, pk):
    document = get_object_or_404(Document, pk=pk)
    if not _may_see(request, document):
        raise Http404
    back = _back(request)
    form = DocumentForm(request.POST or None, instance=document)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        services.update_document(
            document, title=d["title"], description=d["description"], doc_type=d["doc_type"]
        )
        messages.success(request, f"{document} saved.")
        return redirect(back)
    return render(
        request, "files/document_form.html", {"form": form, "document": document, "back": back}
    )


# ---------------------------------------------------------------------------
# Document types (Settings)
# ---------------------------------------------------------------------------


class DocumentTypeForm(forms.ModelForm):
    class Meta:
        model = DocumentType
        fields = ["name", "description", "extensions", "show_on_traveler", "position", "is_active"]

    def clean_name(self):
        # The database rule covers live types only, which the form cannot check itself.
        name = self.cleaned_data["name"].strip()
        if DocumentType.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise ValidationError("There is already a type with this name.")
        return name


@permission_required("files.manage_types")
def type_list(request):
    types = DocumentType.objects.annotate(
        file_count=Count("documents", filter=Q(documents__deleted_at=None))
    )
    return render(request, "files/types.html", {"types": types})


@permission_required("files.manage_types")
def type_edit(request, pk=None):
    doc_type = get_object_or_404(DocumentType, pk=pk) if pk else DocumentType()
    before = audit.snapshot(doc_type) if pk else None
    form = DocumentTypeForm(request.POST or None, instance=doc_type)
    if request.method == "POST" and form.is_valid():
        services.save_type(form.save(commit=False), before=before)
        messages.success(request, f"{doc_type} saved.")
        return redirect("files:types")
    title = f"Edit {doc_type}" if pk else "Add document type"
    return render(request, "files/type_form.html", {"form": form, "title": title})
