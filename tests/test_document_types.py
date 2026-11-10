"""Document types: what each attached file is for (customer sketch, laser file, receipt)."""

import json
import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from trammel.accounts.models import ApiKey
from trammel.core import access
from trammel.files import seed, services
from trammel.files.models import Document, DocumentType


def _upload(name, data=b"x", **kwargs):
    return services.upload(SimpleUploadedFile(name, data), name=name, **kwargs)


@pytest.mark.django_db
def test_defaults_are_seeded_once_and_admin_changes_survive(setup_done):
    laser = DocumentType.objects.get(slug="laser")
    laser.name, laser.is_active = "LightBurn file", False
    laser.save()
    seed.seed()
    seed.seed()
    assert DocumentType.objects.filter(slug="laser").count() == 1
    laser.refresh_from_db()
    assert laser.name == "LightBurn file" and not laser.is_active
    assert DocumentType.objects.count() == len(seed.DEFAULT_TYPES)


@pytest.mark.django_db
def test_upload_suggests_a_type_from_the_file_name(admin, customer):
    assert _upload("coaster.LBRN2", attach_to=customer).document.doc_type.slug == "laser"
    assert _upload("bracket.dxf", b"0\nSECTION").document.doc_type.slug == "cad"
    assert _upload("notes.txt", b"notes").document.doc_type is None
    receipt = DocumentType.objects.get(slug="receipt")
    assert _upload("scan.pdf", b"%PDF-1.4", doc_type=receipt).document.doc_type == receipt
    # A type switched off is no longer suggested.
    DocumentType.objects.filter(slug="cad").update(is_active=False)
    assert _upload("other.dxf", b"0\nSECTION 2").document.doc_type is None


@pytest.mark.django_db
def test_record_page_groups_files_by_type_and_uploads_with_a_type(admin_client, admin, customer):
    _upload("sign.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>', attach_to=customer)
    _upload("notes.txt", b"notes", attach_to=customer)
    page = admin_client.get(f"/customers/{customer.pk}/?tab=files").content.decode()
    assert '<h3 class="file-group">Laser file</h3>' in page
    assert '<h3 class="file-group">Other files</h3>' in page
    photo = DocumentType.objects.get(slug="photo")
    admin_client.post(
        f"/files/attach/customers.customer/{customer.pk}/",
        {"file": SimpleUploadedFile("sketch.txt", b"napkin"), "doc_type": str(photo.pk)},
    )
    assert Document.objects.get(title="sketch.txt").doc_type == photo


@pytest.mark.django_db
def test_editing_a_document(admin_client, admin, customer):
    doc = _upload("a.txt", b"a", attach_to=customer).document
    sketch = DocumentType.objects.get(slug="sketch")
    back = f"/customers/{customer.pk}/"
    response = admin_client.post(
        f"/files/{doc.pk}/edit/",
        {
            "title": "Napkin sketch",
            "doc_type": sketch.pk,
            "description": "From the first call",
            "next": back,
        },
    )
    assert response.status_code == 302 and response.url == back
    doc.refresh_from_db()
    assert (doc.title, doc.doc_type, doc.description) == (
        "Napkin sketch",
        sketch,
        "From the first call",
    )


@pytest.mark.django_db
def test_types_are_managed_in_settings(admin_client, client, setup_done):
    page = admin_client.get("/files/types/").content.decode()
    assert "Production drawing" in page and "<code>.lbrn2</code>" in page
    response = admin_client.post(
        "/files/types/new/",
        {"name": "Jig drawing", "extensions": "jig", "position": "5", "is_active": "on"},
    )
    assert response.status_code == 302
    jig = DocumentType.objects.get(name="Jig drawing")
    assert jig.slug == "jig-drawing" and jig.extension_list() == ["jig"]
    duplicate = admin_client.post("/files/types/new/", {"name": "jig DRAWING", "position": "5"})
    assert "There is already a type with this name." in duplicate.content.decode()


@pytest.mark.django_db
def test_traveler_lists_only_types_meant_for_the_floor(admin_client, admin):
    from trammel.customers.models import Customer
    from trammel.jobs import services as jobs
    from trammel.jobs.models import Job
    from trammel.workorders import services as workorders

    job = jobs.create_job(Job(title="Jig", customer=Customer.internal()))
    jobs.transition(job, "approved", actor=admin)
    wo = workorders.create(job, title="Build the jig")
    _upload("cut.lbrn2", b"<LightBurnProject/>", attach_to=wo)
    _upload("receipt.txt", b"paid", attach_to=wo, doc_type=DocumentType.objects.get(slug="receipt"))
    _upload("untyped.txt", b"loose notes", attach_to=wo)
    page = admin_client.get(f"/work-orders/{wo.pk}/traveler/").content.decode()
    assert "cut.lbrn2 · r1" in page and "untyped.txt" in page
    assert "receipt.txt" not in page


@pytest.mark.django_db
def test_api_reports_and_sets_types(admin, customer):
    token = ApiKey.issue(admin, "t", sorted(access.permissions_of(access.as_actor(admin))))
    token = token[1] if isinstance(token, tuple) else token
    client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")
    types = client.get("/api/v1/files/types").json()
    receipt = next(t for t in types if t["name"] == "Receipt or confirmation")
    response = client.post(
        f"/api/v1/files/customers.customer/{customer.pk}",
        {"file": SimpleUploadedFile("r.txt", b"paid"), "doc_type": receipt["id"]},
    )
    assert response.status_code == 201, response.content
    assert response.json()["doc_type_name"] == "Receipt or confirmation"
    auto = client.post(
        f"/api/v1/files/customers.customer/{customer.pk}",
        {"file": SimpleUploadedFile("cut.lbrn2", b"<LightBurnProject/>")},
    ).json()
    assert auto["doc_type_name"] == "Laser file"
    listed = client.get(f"/api/v1/files/customers.customer/{customer.pk}").json()
    assert {f["doc_type_name"] for f in listed} == {"Receipt or confirmation", "Laser file"}
    assert json.loads(json.dumps(listed))  # plain JSON
    assert re.fullmatch(r"[0-9a-f-]{36}", auto["doc_type"])
