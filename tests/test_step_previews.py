"""STEP previews and the 3D viewer for STEP (FR-DOC-4, 0.11.1): read with
OpenCascade, with the file's own edges and exact measurements."""

import math

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from tests import file_samples as samples
from trammel.core import tasks
from trammel.files import meshes, previews, services, step

pytest.importorskip("OCP", reason="STEP needs OpenCascade (cadquery-ocp-novtk)")


def _made(tmp_path, data, name="part.step"):
    path = tmp_path / name
    path.write_bytes(data)
    return previews.make("step", path)


def test_a_step_box_is_measured_on_its_geometry(tmp_path):
    made = _made(tmp_path, samples.step())
    info = made.model
    assert info["size"] == pytest.approx([10, 20, 30])
    assert info["volume"] == pytest.approx(6000) and info["area"] == pytest.approx(2200)
    assert info["solid"] is True and info["inverted"] is False
    assert info["unit"] == "millimeter" and info["objects"] == 1
    # A box's own 12 edges, each one straight segment.
    assert info["edges"] == 12
    assert made.picture[1] == "webp" and made.mesh


def test_a_hole_is_exact_and_its_seam_isnt_an_edge(tmp_path):
    made = _made(tmp_path, samples.step(size=(40, 40, 10), hole=10))
    info = made.model
    # Exact, not the triangles' approximation of the hole.
    assert info["volume"] == pytest.approx(40 * 40 * 10 - math.pi * 5**2 * 10, rel=1e-7)
    triangles, edges, _low, _size = meshes.unpack(made.mesh)
    # The box's 12 edges, plus the hole's top and bottom circles; the line
    # where the hole's face meets itself isn't drawn.
    straight = edges[edges[:, 0, 2] != edges[:, 1, 2]]
    assert len(straight) == 4  # the box's four upright edges only
    assert info["edges"] > 12


def test_a_step_file_in_inches_says_so_and_is_measured_in_millimeters(tmp_path):
    made = _made(tmp_path, samples.step(size=(25.4, 50.8, 12.7), unit="INCH"))
    assert made.model["unit"] == "inch"
    assert made.model["size"] == pytest.approx([25.4, 50.8, 12.7])


def test_without_opencascade_a_step_file_has_no_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(step, "available", lambda: False)
    assert _made(tmp_path, samples.step()) is None


def test_an_unreadable_step_file_fails_alone(tmp_path):
    with pytest.raises(ValueError):
        _made(tmp_path, b"ISO-10303-21;\nHEADER;\nnot really;\n")


@pytest.mark.django_db
def test_step_files_stored_before_get_previews(admin):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from trammel.core.models import BackgroundTask
    from trammel.files.models import Blob

    old = Blob.objects.create(
        sha256="d" * 64, size=1, mime="model/step", kind="step", path="x", thumb_state="none"
    )
    other = Blob.objects.create(
        sha256="e" * 64, size=1, mime="text/plain", kind="text", path="y", thumb_state="none"
    )
    executor = MigrationExecutor(connection)
    module = __import__("trammel.files.migrations.0006_step_previews", fromlist=["queue"])
    module.queue(executor.loader.project_state().apps, None)
    old.refresh_from_db()
    other.refresh_from_db()
    assert old.thumb_state == "pending" and other.thumb_state == "none"
    assert BackgroundTask.objects.filter(name="files.thumbnail", payload={"blob": str(old.pk)})


@pytest.mark.django_db
def test_a_step_file_gets_a_picture_and_opens_in_the_viewer(
    admin_client, admin, customer, django_capture_on_commit_callbacks
):
    data = samples.step(size=(25.4, 50.8, 12.7), unit="INCH")
    with django_capture_on_commit_callbacks(execute=True):
        rev = services.upload(
            SimpleUploadedFile("bracket.step", data),
            name="bracket.step",
            attach_to=customer,
            user=admin,
        )
    tasks.run_pending()
    blob = rev.blob
    blob.refresh_from_db()
    assert (blob.kind, blob.thumb_state) == ("step", "ready")
    assert blob.thumbnail.endswith(".webp") and blob.mesh.endswith(".mesh.gz")
    page = admin_client.get(f"/customers/{customer.pk}/?tab=files").content.decode()
    assert f'data-model="/files/{rev.document.pk}/model/"' in page
    assert "1 × 2 × 0.5 in" in page
    assert admin_client.get(f"/files/{rev.document.pk}/model/").status_code == 200
