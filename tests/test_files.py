"""FR-DOC: files, revisions, dedupe, type sniffing, GPS stripping, access."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image

from tests.conftest import make_user
from trammel.core import tasks
from trammel.files import services, sniff
from trammel.files.models import Attachment, Blob, Document


def jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (40, 30), "red")
    exif = Image.Exif()
    exif[0x010F] = "TestCam"  # Make
    exif[0x8825] = {1: "N", 2: (47.0, 15.0, 0.0)}  # GPS IFD
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def test_sniffing_ignores_the_filename():
    assert sniff.detect(b"%PDF-1.7\n...", "drawing.png").kind == "pdf"
    assert sniff.detect(b"\x89PNG\r\n\x1a\n....", "x.pdf").mime == "image/png"
    assert sniff.detect(b"  0\nSECTION\n  2\nHEADER\n", "part.txt").kind == "dxf"
    assert sniff.detect(b'<?xml version="1.0"?><svg xmlns="..."></svg>', "a").kind == "svg"
    assert (
        sniff.detect(b'<?xml version="1.0"?><LightBurnProject AppVersion="1.4">', "j").kind
        == "lightburn"
    )
    assert sniff.detect(b"G21\nG90\nG1 X10 Y10\nM5\n", "cut.txt").kind == "gcode"
    assert sniff.detect(b"solid part\n facet normal 0 0 1\n", "p").kind == "stl"
    binary_stl = b"\x00" * 80 + (1).to_bytes(4, "little") + b"\x00" * 50
    assert sniff.detect(binary_stl, "p.bin", size=len(binary_stl)).kind == "stl"
    assert sniff.detect(b"\x00\x01\x02binary", "x").kind == "other"


def test_gps_is_stripped_without_reencoding():
    original = jpeg_with_gps()
    stripped = sniff.strip_jpeg_gps(original)
    with Image.open(io.BytesIO(stripped)) as img:
        exif = img.getexif()
        assert 0x8825 not in exif and exif[0x010F] == "TestCam"
    # The image data after the start-of-scan marker is byte-identical.
    assert original[original.index(b"\xff\xda") :] == stripped[stripped.index(b"\xff\xda") :]
    plain = sniff.strip_jpeg_gps(stripped)
    assert plain is stripped


@pytest.mark.django_db
def test_upload_dedupes_and_versions(admin, customer):
    data = b"%PDF-1.4 fake drawing"
    rev1 = services.upload(
        SimpleUploadedFile("a.pdf", data), name="a.pdf", attach_to=customer, user=admin
    )
    services.upload(SimpleUploadedFile("copy.pdf", data), name="copy.pdf", user=admin)
    assert Blob.objects.count() == 1  # same bytes, stored once
    rev2 = services.upload(
        SimpleUploadedFile("a-v2.pdf", b"%PDF-1.4 revised"),
        name="a-v2.pdf",
        document=rev1.document,
        user=admin,
    )
    doc = Document.objects.get(pk=rev1.document.pk)
    assert rev2.number == 2 and doc.current == rev2
    assert list(services.attachments_for(customer).values_list("document", flat=True)) == [doc.pk]


@pytest.mark.django_db
def test_gps_stripped_on_upload_and_thumbnail_generated(
    admin, customer, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        rev = services.upload(
            SimpleUploadedFile("p.jpg", jpeg_with_gps()),
            name="p.jpg",
            attach_to=customer,
            user=admin,
        )
    stored = services.blob_path(rev.blob).read_bytes()
    with Image.open(io.BytesIO(stored)) as img:
        assert 0x8825 not in img.getexif()
    tasks.run_pending()
    rev.blob.refresh_from_db()
    assert rev.blob.thumb_state == "ready"
    assert (services.media_root() / rev.blob.thumbnail).exists()


@pytest.mark.django_db
@override_settings(MAX_UPLOAD_MB=0)
def test_size_limit(admin, customer):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        services.upload(SimpleUploadedFile("big.bin", b"x"), name="big.bin", user=admin)


@pytest.mark.django_db
def test_detach_keeps_the_file(admin_client, admin, customer):
    rev = services.upload(
        SimpleUploadedFile("n.txt", b"notes"), name="n.txt", attach_to=customer, user=admin
    )
    link = Attachment.objects.get()
    admin_client.post(f"/files/link/{link.pk}/detach/")
    assert not Attachment.objects.exists()
    assert Document.objects.filter(pk=rev.document.pk).exists()


@pytest.mark.django_db
def test_views_upload_and_download(admin_client, customer):
    response = admin_client.post(
        f"/files/attach/customers.customer/{customer.pk}/",
        {"file": SimpleUploadedFile("drawing.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>')},
    )
    assert response.status_code == 302
    rev = Document.objects.get().current
    download = admin_client.get(f"/files/revision/{rev.pk}/?inline=1")
    assert download["Content-Type"] == "image/svg+xml"
    assert "sandbox" in download["Content-Security-Policy"]
    assert download["X-Content-Type-Options"] == "nosniff"
    assert (
        "drawing.svg" in admin_client.get(f"/customers/{customer.pk}/?tab=files").content.decode()
    )


@pytest.mark.django_db
def test_files_need_permission(client, setup_done, customer, admin):
    rev = services.upload(
        SimpleUploadedFile("n.txt", b"secret"), name="n.txt", attach_to=customer, user=admin
    )
    nobody = make_user("nobody@example.com", "customers.view")
    client.force_login(nobody)
    assert client.get(f"/files/revision/{rev.pk}/").status_code == 403
    response = client.post(
        f"/files/attach/customers.customer/{customer.pk}/",
        {"file": SimpleUploadedFile("x.txt", b"x")},
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_cannot_attach_to_unregistered_models(admin_client, admin):
    response = admin_client.post(
        f"/files/attach/accounts.user/{admin.pk}/", {"file": SimpleUploadedFile("x.txt", b"x")}
    )
    assert response.status_code == 404
