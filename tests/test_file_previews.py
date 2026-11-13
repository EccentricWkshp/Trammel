"""Previews of shop files (FR-DOC-4): DXF drawings, STL and 3MF models, and the
thumbnails LightBurn and G-code files carry, made in the background."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from tests import file_samples as samples
from trammel.core import tasks
from trammel.files import previews, services
from trammel.files.models import Blob


def _upload(name, data, customer, admin, capture):
    with capture(execute=True):
        rev = services.upload(
            SimpleUploadedFile(name, data), name=name, attach_to=customer, user=admin
        )
    tasks.run_pending()
    rev.blob.refresh_from_db()
    return rev


def _pixel(data: bytes):
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB").getpixel((image.width // 2, image.height // 2))


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "data", "kind", "ext"),
    [
        ("bracket.dxf", samples.dxf, "dxf", "svg"),
        ("ring.stl", samples.stl_binary, "stl", "webp"),
        ("ring-ascii.stl", samples.stl_ascii, "stl", "webp"),
        ("ring.3mf", samples.three_mf, "3mf", "webp"),
        ("sign.lbrn2", samples.lightburn, "lightburn", "webp"),
        ("part.gcode", samples.gcode, "gcode", "webp"),
    ],
)
def test_shop_files_get_a_preview(
    admin, customer, django_capture_on_commit_callbacks, name, data, kind, ext
):
    rev = _upload(name, data(), customer, admin, django_capture_on_commit_callbacks)
    blob = rev.blob
    assert (blob.kind, blob.thumb_state) == (kind, "ready")
    assert blob.thumbnail.endswith(f".{ext}")
    stored = (services.media_root() / blob.thumbnail).read_bytes()
    if ext == "svg":
        assert stored.startswith(b"<?xml") and b"<path" in stored and b"<script" not in stored
    else:
        assert Image.open(io.BytesIO(stored)).format == "WEBP"


def test_models_are_drawn_and_saved_thumbnails_are_used():
    import numpy as np

    triangles = np.array(samples._ring(), dtype=float)
    data, _ = previews._model(triangles)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (previews.MODEL_PX, previews.MODEL_PX)
        assert image.convert("RGB").getpixel((5, 5)) == (255, 255, 255)  # background
    # The largest of the G-code's thumbnails, and the 3MF's own, are used.
    path = __import__("pathlib").Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        gcode = path(tmp) / "a.gcode"
        gcode.write_bytes(samples.gcode())
        r, g, b = _pixel(previews.render("gcode", gcode)[0])
        assert r > 150 and b < 100  # the 64×48 orange one, not the blue 16×12
        model = path(tmp) / "a.3mf"
        model.write_bytes(samples.three_mf(thumbnail=True))
        r, g, b = _pixel(previews.render("3mf", model)[0])
        assert g > 120 and r < 80  # the saved green thumbnail, not a render


@pytest.mark.django_db
def test_no_preview_when_there_is_nothing_to_show(
    admin, customer, django_capture_on_commit_callbacks
):
    capture = django_capture_on_commit_callbacks
    for name, data in (
        ("blank.dxf", samples.empty_dxf()),
        ("plain.gcode", samples.gcode(thumbnail=False)),
        ("plain.lbrn2", samples.lightburn(thumbnail=False)),
    ):
        rev = _upload(name, data, customer, admin, capture)
        assert (rev.blob.thumb_state, rev.blob.thumbnail) == ("none", ""), name
    # A broken model fails once, quietly, and isn't tried again.
    rev = _upload("broken.3mf", b"PK\x03\x04" + b"\x00" * 40, customer, admin, capture)
    assert rev.blob.thumb_state in ("none", "failed")


@pytest.mark.django_db
def test_previews_are_served_and_open_from_the_card(
    admin_client, admin, customer, django_capture_on_commit_callbacks
):
    rev = _upload("bracket.dxf", samples.dxf(), customer, admin, django_capture_on_commit_callbacks)
    response = admin_client.get(f"/files/{rev.document.pk}/thumbnail/")
    assert response["Content-Type"] == "image/svg+xml"
    assert "sandbox" in response["Content-Security-Policy"]
    page = admin_client.get(f"/customers/{customer.pk}/?tab=files").content.decode()
    # The picture opens the preview over the page (a new tab without script);
    # a DXF itself only downloads.
    assert (
        f'href="/files/{rev.document.pk}/thumbnail/" target="_blank" rel="noopener" '
        'data-lightbox="bracket.dxf"'
    ) in page
    assert "previews.js" in page


@pytest.mark.django_db
def test_existing_files_are_queued_for_previews(admin, customer):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    blob = Blob.objects.create(
        sha256="a" * 64, size=1, mime="model/stl", kind="stl", path="x", thumb_state="none"
    )
    executor = MigrationExecutor(connection)
    module = __import__("trammel.files.migrations.0004_previews", fromlist=["queue"])
    module.queue(executor.loader.project_state().apps, None)
    blob.refresh_from_db()
    assert blob.thumb_state == "pending"
    from trammel.core.models import BackgroundTask

    assert BackgroundTask.objects.filter(name="files.thumbnail", payload={"blob": str(blob.pk)})


@pytest.mark.django_db
def test_the_traveler_pdf_carries_drawing_and_model_previews(
    admin_client, admin, django_capture_on_commit_callbacks
):
    from trammel.core import templating
    from trammel.customers.models import Customer
    from trammel.jobs import services as jobs
    from trammel.jobs.models import Job
    from trammel.workorders import services as wos

    # The shop's own job: no quote or agreement needed for its work order.
    job = jobs.create_job(Job(title="Bracket", customer=Customer.internal()))
    capture = django_capture_on_commit_callbacks
    for name, data in (("bracket.dxf", samples.dxf()), ("ring.stl", samples.stl_binary())):
        with capture(execute=True):
            services.upload(SimpleUploadedFile(name, data), name=name, attach_to=job, user=admin)
    tasks.run_pending()
    jobs.transition(job, "approved", actor=admin)
    wo = wos.create(job)
    html = admin_client.get(f"/work-orders/{wo.pk}/traveler/").content.decode()
    assert "data:image/svg+xml;base64," in html and "data:image/webp;base64," in html
    try:
        templating.render_pdf("<p>x</p>")
    except templating.PdfUnavailable:
        pytest.skip("WeasyPrint's system libraries aren't installed here (they are in CI).")
    response = admin_client.get(f"/work-orders/{wo.pk}/traveler/?format=pdf")
    assert response["Content-Type"] == "application/pdf"
    assert response.content.startswith(b"%PDF") and b"/Image" in response.content


@pytest.mark.django_db
def test_a_page_waits_for_a_preview_and_shows_it_when_ready(
    admin_client, admin, customer, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        rev = services.upload(
            SimpleUploadedFile("ring.stl", samples.stl_binary()),
            name="ring.stl",
            attach_to=customer,
            user=admin,
        )
    status = f"/files/{rev.document.pk}/thumbnail/status/"
    page = admin_client.get(f"/customers/{customer.pk}/?tab=files").content.decode()
    # Not made yet: a placeholder that asks until it is.
    assert f'data-preview-wait="{status}"' in page and "Making the preview" in page
    assert admin_client.get(status).json() == {"ready": False, "waiting": True}
    tasks.run_pending()
    response = admin_client.get(status)
    assert response.json() == {"ready": True, "waiting": False}
    assert response["Cache-Control"] == "no-store"
    page = admin_client.get(f"/customers/{customer.pk}/?tab=files").content.decode()
    assert "data-preview-wait" not in page and 'data-lightbox="ring.stl"' in page


def test_a_huge_pdf_page_is_drawn_small(tmp_path, monkeypatch):
    """SEC-02 (0.10.5): a tiny PDF can declare a huge page; it is drawn at a
    scale from its size, never more than PDF_RENDER_PX on its longest side."""
    import pypdfium2

    assert previews.pdf_scale(612, 792) == 1.5  # a letter page, as before
    assert previews.pdf_scale(0, 0) is None
    path = tmp_path / "huge.pdf"
    pdf = pypdfium2.PdfDocument.new()
    pdf.new_page(14000, 14000)
    pdf.save(str(path))
    pdf.close()
    assert path.stat().st_size < 2000
    drawn = []
    real = pypdfium2.PdfPage.render

    def render(page, **kwargs):
        width, height = page.get_size()
        drawn.append(max(width, height) * kwargs["scale"])
        return real(page, **kwargs)

    monkeypatch.setattr(pypdfium2.PdfPage, "render", render)
    data, ext = previews.render("pdf", path)
    assert ext == "webp" and drawn and drawn[0] <= previews.PDF_RENDER_PX
    assert max(Image.open(io.BytesIO(data)).size) <= previews.THUMB_PX
