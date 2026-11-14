"""The 3D viewer (FR-DOC-4, 0.11.0): models read with their build placed,
measured, and packed for the viewer; served to staff, on cards and the review
of files from customers."""

import gzip
import io
import zipfile

import numpy as np
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from tests import file_samples as samples
from tests.conftest import make_user
from trammel.core import tasks
from trammel.files import meshes, previews, services
from trammel.files.models import Blob


def _made(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return previews.make(name.rsplit(".", 1)[-1], path)


def test_a_box_is_measured(tmp_path):
    made = _made(tmp_path, "box.stl", samples.stl_of(samples.box_triangles()))
    info = made.model
    assert info["size"] == [10, 20, 30]
    assert info["volume"] == pytest.approx(6000)
    assert info["area"] == pytest.approx(2 * (200 + 300 + 600))
    assert info["solid"] is True and info["inverted"] is False
    # STL names no unit.
    assert info["unit"] == "" and info["objects"] == 1
    # A box's 12 edges are sharp; the diagonals across its faces aren't.
    assert info["edges"] == 12
    assert made.picture[1] == "webp"


def test_the_viewer_mesh_reads_back(tmp_path):
    made = _made(tmp_path, "box.stl", samples.stl_of(samples.box_triangles(at=(5, -5, 2))))
    triangles, edges, low, size = meshes.unpack(made.mesh)
    assert len(triangles) == 12 and len(edges) == 12
    assert low.tolist() == [5, -5, 2] and size.tolist() == [10, 20, 30]
    # Within the packing's step (1/65535 of the size).
    corners = sorted({tuple(np.round(p, 3)) for p in triangles.reshape(-1, 3)})
    assert corners[0] == (5, -5, 2) and corners[-1] == (15, 15, 32)


def test_an_open_or_inside_out_surface_is_flagged(tmp_path):
    open_top = _made(tmp_path, "cup.stl", samples.stl_of(samples.box_triangles(open_top=True)))
    assert open_top.model["solid"] is False
    inside_out = _made(tmp_path, "in.stl", samples.stl_of(samples.box_triangles(inward=True)))
    info = inside_out.model
    assert info["solid"] is True and info["inverted"] is True
    assert info["volume"] == pytest.approx(6000)
    # Wound the other way round, the same edges are sharp.
    assert info["edges"] == 12


def test_a_3mf_places_its_parts_where_its_build_puts_them(tmp_path):
    info = _made(tmp_path, "boxes.3mf", samples.three_mf_boxes()).model
    # One box turned a quarter turn (20 × 10), the other moved 50 along X.
    assert info["size"] == [60, 20, 30]
    assert info["objects"] == 2 and info["unit"] == "millimeter"
    assert info["volume"] == pytest.approx(12000) and info["solid"] is True


def test_a_3mf_in_inches_is_measured_in_millimeters(tmp_path):
    info = _made(tmp_path, "boxes.3mf", samples.three_mf_boxes(unit="inch")).model
    assert info["unit"] == "inch"
    assert info["size"] == pytest.approx([60 * 25.4, 20 * 25.4, 30 * 25.4])


def test_a_3mf_part_kept_in_its_own_file_is_read(tmp_path):
    with zipfile.ZipFile(io.BytesIO(samples.three_mf_production())) as zf:
        model = meshes.read_3mf(zf)
    assert model.objects == 1
    low = model.triangles.reshape(-1, 3).min(axis=0)
    assert low.tolist() == [0, 0, 5]


def test_a_3mf_declaring_a_doctype_is_not_read(tmp_path):
    assert _made(tmp_path, "x.3mf", samples.three_mf_boxes(doctype=True)) is None


def test_a_3mf_thumbnail_is_still_its_picture(tmp_path):
    made = _made(tmp_path, "ring.3mf", samples.three_mf(thumbnail=True))
    with zipfile.ZipFile(io.BytesIO(samples.three_mf(thumbnail=True))) as zf:
        assert made.picture == previews._3mf_thumbnail(zf)
    assert made.mesh and made.model["triangles"] == 576


def test_a_large_model_is_simplified_for_the_viewer_but_measured_whole(tmp_path, monkeypatch):
    monkeypatch.setattr(meshes, "VIEW_TRIANGLES", 200)
    made = _made(tmp_path, "ring.stl", samples.stl_binary())
    info = made.model
    assert info["triangles"] == 576 and 0 < info["shown"] <= 200
    triangles, *_ = meshes.unpack(made.mesh)
    assert len(triangles) == info["shown"]
    whole = _made(tmp_path, "ring2.stl", samples.stl_binary()).model
    assert whole["volume"] == info["volume"] and whole["area"] == info["area"]


def test_a_truncated_stl_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(meshes, "MAX_TRIANGLES", 100)
    info = _made(tmp_path, "ring.stl", samples.stl_binary()).model
    assert info["truncated"] is True and info["triangles"] <= 100


# ---------------------------------------------------------------------------
# Served to staff
# ---------------------------------------------------------------------------


def _upload(name, data, target, admin, capture):
    with capture(execute=True):
        rev = services.upload(
            SimpleUploadedFile(name, data), name=name, attach_to=target, user=admin
        )
    tasks.run_pending()
    rev.blob.refresh_from_db()
    return rev


@pytest.mark.django_db
def test_a_model_opens_in_the_viewer_from_its_card(
    admin_client, admin, customer, django_capture_on_commit_callbacks
):
    data = samples.stl_of(samples.box_triangles((25.4, 50.8, 12.7)))
    rev = _upload("block.stl", data, customer, admin, django_capture_on_commit_callbacks)
    blob = rev.blob
    assert blob.thumb_state == "ready" and blob.mesh.endswith(".mesh.gz")
    assert blob.model["size"] == pytest.approx([25.4, 50.8, 12.7])

    response = admin_client.get(f"/files/{rev.document.pk}/model/")
    assert response.status_code == 200
    assert response["Content-Encoding"] == "gzip"
    assert response["Content-Type"] == "application/octet-stream"
    body = b"".join(response.streaming_content)
    assert gzip.decompress(body).startswith(meshes.MAGIC)

    page = admin_client.get(f"/customers/{customer.pk}/?tab=files").content.decode()
    assert f'data-model="/files/{rev.document.pk}/model/"' in page
    assert "data-model-info=" in page and 'data-units="imperial"' in page
    # Its size on the card, in the shop's units.
    assert "1 × 2 × 0.5 in" in page
    assert "viewer.js" in page


@pytest.mark.django_db
def test_a_metric_shop_sees_millimeters(
    admin_client, admin, customer, django_capture_on_commit_callbacks
):
    from trammel.core.models import BusinessProfile

    BusinessProfile.objects.update(unit_system="metric")
    data = samples.stl_of(samples.box_triangles())
    _upload("box.stl", data, customer, admin, django_capture_on_commit_callbacks)
    page = admin_client.get(f"/customers/{customer.pk}/?tab=files").content.decode()
    assert "10 × 20 × 30 mm" in page and 'data-units="metric"' in page


@pytest.mark.django_db
def test_only_staff_who_may_see_the_file_get_its_model(
    client, admin, customer, django_capture_on_commit_callbacks
):
    rev = _upload(
        "box.stl",
        samples.stl_of(samples.box_triangles()),
        customer,
        admin,
        django_capture_on_commit_callbacks,
    )
    url = f"/files/{rev.document.pk}/model/"
    assert client.get(url).status_code in (302, 403)
    client.force_login(make_user("nofiles@example.com", "customers.view"))
    assert client.get(url).status_code in (403, 404)
    client.force_login(make_user("files@example.com", "customers.view", "files.view"))
    assert client.get(url).status_code == 200


@pytest.mark.django_db
def test_files_that_arent_models_have_no_viewer(
    admin_client, admin, customer, django_capture_on_commit_callbacks
):
    rev = _upload("bracket.dxf", samples.dxf(), customer, admin, django_capture_on_commit_callbacks)
    assert rev.blob.mesh == "" and rev.blob.model is None
    assert admin_client.get(f"/files/{rev.document.pk}/model/").status_code == 404
    page = admin_client.get(f"/customers/{customer.pk}/?tab=files").content.decode()
    assert "data-model=" not in page


@pytest.mark.django_db
def test_the_api_lists_a_models_measurements(admin, customer, django_capture_on_commit_callbacks):
    from django.test import Client

    from trammel.accounts.models import ApiKey

    _upload(
        "box.stl",
        samples.stl_of(samples.box_triangles()),
        customer,
        admin,
        django_capture_on_commit_callbacks,
    )
    reader = make_user("reader@example.com", "customers.view", "files.view")
    issued = ApiKey.issue(reader, "Test", ["customers.view", "files.view"])
    token = issued[1] if isinstance(issued, tuple) else issued
    api = Client(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = api.get(f"/api/v1/files/customers.customer/{customer.pk}")
    assert response.status_code == 200
    (row,) = response.json()
    assert row["model"]["size"] == [10, 20, 30] and row["model"]["solid"] is True


@pytest.mark.django_db
def test_models_stored_before_are_read_again(admin, customer):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from trammel.core.models import BackgroundTask

    stl = Blob.objects.create(
        sha256="b" * 64, size=1, mime="model/stl", kind="stl", path="x", thumb_state="ready"
    )
    pdf = Blob.objects.create(
        sha256="c" * 64, size=1, mime="application/pdf", kind="pdf", path="y", thumb_state="ready"
    )
    executor = MigrationExecutor(connection)
    module = __import__("trammel.files.migrations.0005_model_viewer", fromlist=["queue"])
    module.queue(executor.loader.project_state().apps, None)
    stl.refresh_from_db()
    pdf.refresh_from_db()
    assert stl.thumb_state == "pending" and pdf.thumb_state == "ready"
    assert BackgroundTask.objects.filter(name="files.thumbnail", payload={"blob": str(stl.pk)})
