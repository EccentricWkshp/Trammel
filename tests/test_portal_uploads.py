# ruff: noqa: F811 — the portal fixtures come from test_portal
"""Files customers send through the portal (FR-PRT-7): held for review, attached
to nothing, until staff accept them onto a job or the customer, or decline them."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image

from tests import file_samples as samples
from tests.conftest import make_user
from tests.test_portal import _emailed_code, grace, portal_on, signed_in, smtp  # noqa: F401
from trammel.core import tasks
from trammel.core.models import AuditEvent
from trammel.customers.models import Customer
from trammel.files.models import Attachment, Blob, DocumentType
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.notifications.models import EmailLog
from trammel.portal import uploads
from trammel.portal.models import PortalSettings, Upload


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 30), "green").save(out, "JPEG")
    return out.getvalue()


def _files(*pairs):
    return [SimpleUploadedFile(name, data) for name, data in pairs]


def _send(client, capture, files, **extra):
    with capture(execute=True):
        return client.post("/portal/send/", {"files": files, **extra})


@pytest.fixture
def job(grace):
    return jobs.create_job(Job(title="Sign", customer=grace))


@pytest.mark.django_db
def test_a_customer_sends_files_for_a_job(
    signed_in,
    grace,
    job,
    admin,
    django_capture_on_commit_callbacks,
):
    page = signed_in.get(f"/portal/jobs/{job.pk}/").content.decode()
    assert "Send us files for this job" in page
    response = _send(
        signed_in,
        django_capture_on_commit_callbacks,
        _files(("logo.dxf", samples.dxf()), ("photo.jpg", _jpeg())),
        job=str(job.pk),
        note="The logo, and where it goes.",
    )
    assert response["Location"] == f"/portal/jobs/{job.pk}/#send-files"
    sent = list(Upload.objects.order_by("sent_at"))
    assert [(u.job, u.status, u.note, u.sent_by) for u in sent] == [
        (job, "pending", "The logo, and where it goes.", "Grace Hopper")
    ] * 2
    # Held: attached to nothing yet.
    assert not Attachment.objects.exists()
    page = signed_in.get(f"/portal/jobs/{job.pk}/").content.decode()
    assert "Waiting for review" in page and "logo.dxf" in page
    # They can have their own file back; staff hear about it.
    assert signed_in.get(f"/portal/sent/{sent[0].pk}/").status_code == 200
    email = EmailLog.objects.get(event="portal_files")
    assert email.to == "admin@example.com" and "Files from Grace Hopper" in email.subject
    assert AuditEvent.objects.filter(action="portal.upload").exists()


@pytest.mark.django_db
def test_only_shop_files_within_the_limits_are_taken(
    signed_in,
    grace,
    django_capture_on_commit_callbacks,
):
    blobs = Blob.objects.count()
    response = _send(
        signed_in,
        django_capture_on_commit_callbacks,
        _files(
            ("part.gcode", samples.gcode()),
            ("stuff.zip", b"PK\x03\x04" + b"\x00" * 60),
            ("setup.exe", b"MZ\x90\x00" + b"\x00" * 60),
        ),
    )
    messages = [str(m) for m in response.wsgi_request._messages]
    assert len(messages) == 3 and all("can't be sent here" in m for m in messages)
    assert Blob.objects.count() == blobs and not Upload.objects.exists()

    settings = PortalSettings.load()
    settings.upload_limit_mb = 1
    settings.save()
    big = b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 10)
    _send(signed_in, django_capture_on_commit_callbacks, _files(("big.pdf", big)))
    assert not Upload.objects.exists()

    # Ten at a time, and no more than fifty waiting.
    many = _files(*((f"p{i}.jpg", _jpeg()) for i in range(uploads.PER_SEND + 2)))
    _send(signed_in, django_capture_on_commit_callbacks, many)
    assert Upload.objects.count() == uploads.PER_SEND
    Upload.objects.bulk_create(
        Upload(customer=grace, document=Upload.objects.first().document, sent_by="x")
        for _ in range(uploads.WAITING_PER_CUSTOMER)
    )
    assert uploads.room_for(grace) == 0
    response = _send(signed_in, django_capture_on_commit_callbacks, _files(("one.jpg", _jpeg())))
    assert any("as many files as we can take" in str(m) for m in response.wsgi_request._messages)


@pytest.mark.django_db
def test_staff_accept_a_file_onto_a_job(
    signed_in,
    grace,
    job,
    admin_client,
    django_capture_on_commit_callbacks,
):
    _send(
        signed_in,
        django_capture_on_commit_callbacks,
        _files(("bracket.dxf", samples.dxf())),
    )
    tasks.run_pending()
    sent = Upload.objects.get()
    assert sent.job is None  # sent from the account page
    page = admin_client.get("/").content.decode()
    assert "Files from customers" in page and "bracket.dxf" in page
    page = admin_client.get(f"/customers/{grace.pk}/").content.decode()
    assert "Files the customer sent" in page and "1 to review" in page
    page = admin_client.get(f"/portal/review/{sent.pk}/").content.decode()
    assert "bracket.dxf" in page and f"/files/{sent.document.pk}/thumbnail/" in page
    drawing = DocumentType.objects.filter(slug="production_drawing").first()
    admin_client.post(
        f"/portal/review/{sent.pk}/",
        {
            "action": "accept",
            "target": str(job.pk),
            "title": "Bracket drawing",
            "doc_type": str(drawing.pk) if drawing else "",
            "shared": "1",
        },
    )
    sent.refresh_from_db()
    assert sent.status == "accepted" and sent.reviewed_by is not None
    link = Attachment.objects.get()
    assert (link.target, link.shared_with_customer, link.document.title) == (
        job,
        True,
        "Bracket drawing",
    )
    # The customer sees it with the job's files, with its preview, and as received.
    page = signed_in.get(f"/portal/jobs/{job.pk}/").content.decode()
    assert "Bracket drawing" in page and f"/portal/files/{link.document.pk}/thumbnail/" in page
    assert signed_in.get(f"/portal/files/{link.document.pk}/thumbnail/").status_code == 200
    assert 'data-lightbox="Bracket drawing"' in page and "previews.js" in page
    assert "Received" in signed_in.get("/portal/").content.decode()


@pytest.mark.django_db
def test_staff_decline_with_a_reason_the_customer_sees(
    signed_in,
    grace,
    admin_client,
    django_capture_on_commit_callbacks,
):
    _send(signed_in, django_capture_on_commit_callbacks, _files(("blurry.jpg", _jpeg())))
    sent = Upload.objects.get()
    admin_client.post(
        f"/portal/review/{sent.pk}/",
        {"action": "decline", "reason": "It's blurry; please send a sharper photo."},
    )
    sent.refresh_from_db()
    assert sent.status == "declined" and sent.document.deleted_at is not None
    page = signed_in.get("/portal/").content.decode()
    assert "Not accepted" in page and "please send a sharper photo" in page
    assert signed_in.get(f"/portal/sent/{sent.pk}/").status_code == 404
    # Reviewed once only.
    response = admin_client.post(f"/portal/review/{sent.pk}/", {"action": "accept"})
    assert response.status_code == 200 and not Attachment.objects.exists()


@pytest.mark.django_db
def test_each_customer_sees_only_their_own(
    signed_in,
    grace,
    job,
    django_capture_on_commit_callbacks,
):
    other = Customer.objects.create(name="Alan Turing", email="alan@example.com")
    theirs = jobs.create_job(Job(title="Desk", customer=other))
    response = _send(
        signed_in,
        django_capture_on_commit_callbacks,
        _files(("a.jpg", _jpeg())),
        job=str(theirs.pk),
    )
    assert response.status_code == 404 and not Upload.objects.exists()
    _send(signed_in, django_capture_on_commit_callbacks, _files(("a.jpg", _jpeg())))
    mine = Upload.objects.get()
    mine.customer = other
    mine.save()
    assert signed_in.get(f"/portal/sent/{mine.pk}/").status_code == 404


@pytest.mark.django_db
def test_who_can_review_and_when_customers_can_send(
    signed_in,
    grace,
    admin_client,
    django_capture_on_commit_callbacks,
):
    _send(signed_in, django_capture_on_commit_callbacks, _files(("a.jpg", _jpeg())))
    sent = Upload.objects.get()
    viewer = Client()
    viewer.force_login(make_user("viewer@example.com", "customers.view"))
    assert viewer.get(f"/portal/review/{sent.pk}/").status_code == 403
    # Turned off on the System page: no form, and nothing taken.
    page = admin_client.get("/settings/system/").content.decode()
    assert "Files from customers" in page
    admin_client.post("/portal/upload-settings/", {"upload_limit_mb": "10"})
    assert not PortalSettings.load().uploads_enabled
    page = signed_in.get("/portal/").content.decode()
    assert 'action="/portal/send/"' not in page and "Files you've sent" in page
    response = _send(signed_in, django_capture_on_commit_callbacks, _files(("b.jpg", _jpeg())))
    assert any("isn't available" in str(m) for m in response.wsgi_request._messages)
    response = admin_client.post(
        "/portal/upload-settings/", {"uploads_enabled": "on", "upload_limit_mb": "100000"}
    )
    assert PortalSettings.load().upload_limit_mb == 10  # refused: larger than staff uploads


@pytest.mark.django_db
def test_nothing_is_sent_while_staff_view_as_the_customer(
    admin_client,
    grace,
    django_capture_on_commit_callbacks,
):
    admin_client.post(f"/portal/preview/{grace.pk}/")
    page = admin_client.get("/portal/").content.decode()
    assert "Send us files" in page
    _send(admin_client, django_capture_on_commit_callbacks, _files(("a.jpg", _jpeg())))
    assert not Upload.objects.exists()
    assert Client().get("/portal/review/").status_code == 302  # staff sign-in first


@pytest.mark.django_db
def test_pictures_and_pdfs_can_be_viewed_in_the_browser(
    signed_in, grace, django_capture_on_commit_callbacks
):
    _send(signed_in, django_capture_on_commit_callbacks, _files(("photo.jpg", _jpeg())))
    tasks.run_pending()
    sent = Upload.objects.get()
    page = signed_in.get("/portal/").content.decode()
    assert (
        f'href="/portal/sent/{sent.pk}/?inline=1"' in page and 'data-lightbox="photo.jpg"' in page
    )
    assert "Waiting for review" in page
    inline = signed_in.get(f"/portal/sent/{sent.pk}/?inline=1")
    assert inline["Content-Disposition"].startswith("inline")
    assert signed_in.get(f"/portal/sent/{sent.pk}/")["Content-Disposition"].startswith("attachment")


@pytest.mark.django_db
def test_files_waiting_for_review_are_flagged_on_the_job(
    signed_in,
    grace,
    job,
    admin_client,
    django_capture_on_commit_callbacks,
):
    other = jobs.create_job(Job(title="Other", customer=grace))
    _send(
        signed_in,
        django_capture_on_commit_callbacks,
        _files(("logo.dxf", samples.dxf()), ("photo.jpg", _jpeg())),
        job=str(job.pk),
    )
    first = Upload.objects.order_by("sent_at").first()
    review = f"/portal/review/{first.pk}/"
    page = admin_client.get(f"/jobs/{job.pk}/").content.decode()
    header = page[: page.index("tab-body")]
    assert "2 files from the customer to review" in header and review in header
    # The panel leads the overview while files wait.
    overview = page[page.index("tab-body") :]
    assert overview.index("Files the customer sent") < overview.index("Scope")
    listing = admin_client.get("/jobs/").content.decode()
    assert "2 files from the customer to review" in listing and review in listing
    assert "to review" not in admin_client.get(f"/jobs/{other.pk}/").content.decode()

    for upload in Upload.objects.all():
        uploads.decline(upload, reason="Not needed")
    page = admin_client.get(f"/jobs/{job.pk}/").content.decode()
    assert "from the customer to review" not in page
    overview = page[page.index("tab-body") :]
    assert overview.index("Scope") < overview.index("Files the customer sent")
