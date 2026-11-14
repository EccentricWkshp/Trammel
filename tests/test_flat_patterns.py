"""Flat patterns of sheet metal parts (FR-DOC-4, 0.11.2): STEP parts unfolded,
laid out for their material's K-factor, and shown, priced and sent as DXF."""

import io
import math
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from tests import file_samples as samples
from tests.conftest import make_user
from trammel.core import tasks
from trammel.files import flat, services, step, unfold
from trammel.shop import sheet
from trammel.shop.models import Material

pytest.importorskip("OCP", reason="unfolding needs OpenCascade (cadquery-ocp-novtk)")

T = 4.7625  # 3/16 in


def _unfold(tmp_path, data):
    path = tmp_path / "part.step"
    path.write_bytes(data)
    shape = step._shape(path)
    return unfold.unfold(shape, step._measure(shape)["volume"])


def _length(k, first=60.0, second=40.0, radius=3.0, thickness=T):
    return first + second + math.pi / 2 * (radius + k * thickness)


@pytest.mark.parametrize("k", [0.0, 0.33, 0.44, 0.5])
def test_a_bracket_lays_out_to_its_bend_allowance(tmp_path, k):
    p = flat.pattern(_unfold(tmp_path, samples.bracket()), k)
    assert p.size == pytest.approx([_length(k), 50], abs=1e-6)
    assert p.thickness == pytest.approx(T, abs=1e-6)
    (bend,) = p.bends
    assert bend.angle == pytest.approx(90) and bend.radius == pytest.approx(3)
    # The bend line runs the width of the part; the bend takes up its allowance.
    assert bend.length == pytest.approx(50)
    assert bend.allowance == pytest.approx(math.pi / 2 * (3 + k * T), abs=1e-9)
    # The outline and the hole: two pierces, and every edge of both cut.
    assert p.pierces == 2 and p.holes == 1
    assert p.cut_length == pytest.approx(2 * (_length(k) + 50) + math.pi * 10, abs=1e-6)
    assert p.area == pytest.approx(_length(k) * 50 - math.pi * 25, abs=0.05)
    # The bend line runs across the blank, square to it.
    (ax, _ay), (bx, _by) = bend.line
    assert ax == pytest.approx(bx, abs=1e-6)


@pytest.mark.parametrize("k", [0.0, 0.36, 0.5])
def test_a_channel_unfolds_across_both_its_bends(tmp_path, k):
    """Flanges beyond flanges: each laid flat from the one before (the user's
    own belt grinder leg, with three bends, matched Fusion's flat pattern at
    K 0.36 to 0.0001 in, hole for hole)."""
    allowance = math.pi / 2 * (3 + k * T)
    p = flat.pattern(_unfold(tmp_path, samples.channel()), k)
    assert p.size == pytest.approx([60 + 2 * 30 + 2 * allowance, 50], abs=1e-6)
    first, second = p.bends
    assert first.angle == pytest.approx(90) and second.angle == pytest.approx(90)
    # Both fold the same way, and their lines are the base and a bend apart.
    assert first.up == second.up
    xs = sorted(b.line[0][0] for b in p.bends)
    assert xs[1] - xs[0] == pytest.approx(60 + allowance, abs=1e-6)
    assert p.pierces == 1


def test_its_area_times_its_thickness_is_its_volume_at_k_one_half(tmp_path):
    data = _unfold(tmp_path, samples.bracket())
    path = tmp_path / "again.step"
    path.write_bytes(samples.bracket())
    volume = step._measure(step._shape(path))["volume"]
    assert flat.pattern(data, 0.5).area * T == pytest.approx(volume, rel=1e-5)


def test_a_plate_has_a_flat_pattern_with_no_bends(tmp_path):
    p = flat.pattern(_unfold(tmp_path, samples.step(size=(100, 60, 3))), 0.44)
    assert p.size == pytest.approx([100, 60]) and p.bends == []
    assert p.cut_length == pytest.approx(320) and p.pierces == 1


def test_a_solid_block_isnt_sheet_metal(tmp_path):
    assert _unfold(tmp_path, samples.block()) is None


def test_the_dxf_is_in_the_parts_unit_on_cad_layers(tmp_path):
    import ezdxf

    p = flat.pattern(_unfold(tmp_path, samples.bracket()), 0.44)
    doc = ezdxf.read(io.StringIO(flat.dxf(p, "inch").decode()))
    assert doc.header["$INSUNITS"] == 1
    msp = doc.modelspace()
    layers = {e.dxf.layer for e in msp}
    assert {"OUTER_PROFILES", "INTERIOR_PROFILES", "BEND", "BEND_EXTENT"} <= layers
    (hole,) = msp.query("CIRCLE")
    assert hole.dxf.radius == pytest.approx(5 / 25.4)
    xs = [
        x for e in msp.query("LINE[layer=='OUTER_PROFILES']") for x in (e.dxf.start.x, e.dxf.end.x)
    ]
    assert min(xs) == pytest.approx(0, abs=1e-9)
    assert max(xs) == pytest.approx(_length(0.44) / 25.4, abs=1e-6)


def test_the_picture_marks_each_bend(tmp_path):
    picture = flat.svg(flat.pattern(_unfold(tmp_path, samples.bracket()), 0.44))
    assert picture.startswith("<svg") and "1: UP 90°" in picture or "1: DOWN 90°" in picture
    assert "<script" not in picture


@pytest.mark.parametrize(
    ("text", "mm"),
    [
        ("3 mm", 3.0),
        ("1/8 in", 3.175),
        ('3/16"', 4.7625),
        ("0.1875 in", 4.7625),
        ("1-1/2 in", 38.1),
        ("0.1875", 4.7625),  # the shop's units: inches here
        ("11 ga", None),
        ("", None),
        ("1/0", None),
    ],
)
def test_a_materials_thickness_is_read_from_its_text(text, mm):
    got = sheet.thickness_mm(text)
    assert got == (pytest.approx(mm) if mm is not None else None)


# ---------------------------------------------------------------------------
# In the shop


def _upload(name, data, target, admin, capture):
    with capture(execute=True):
        rev = services.upload(
            SimpleUploadedFile(name, data), name=name, attach_to=target, user=admin
        )
    tasks.run_pending()
    rev.blob.refresh_from_db()
    return rev


@pytest.mark.django_db
def test_a_sheet_metal_part_gets_its_flat_pattern_from_its_material(
    admin_client, admin, customer, django_capture_on_commit_callbacks
):
    rev = _upload(
        "bracket.step", samples.bracket(), customer, admin, django_capture_on_commit_callbacks
    )
    blob = rev.blob
    assert blob.flat.endswith(".flat.json.gz")
    assert blob.model["sheet"] == {"thickness": pytest.approx(T), "bends": 1}
    doc = rev.document
    page = admin_client.get(f"/customers/{customer.pk}/?tab=files").content.decode()
    assert f'href="/files/{doc.pk}/flat/?next=' in page and "1 bend" in page
    assert f'data-flat="/files/{doc.pk}/flat/"' in page

    # No material its thickness has a K-factor: the default, and which to set.
    steel = Material.objects.create(name="Mild steel", category="metal", thickness="3/16 in")
    page = admin_client.get(f"/files/{doc.pk}/flat/").content.decode()
    assert "0.44, Trammel's default" in page and "Mild steel" in page
    assert f"{flat._num(_length(0.44) / 25.4, 3)} × 1.969 in" in page
    assert "Pierces</dt><dd>2 (the outline and 1 hole)" in page
    allowance = math.pi / 2 * (3 + 0.44 * T)
    assert "Bend line length" in page and "Bend allowance" in page
    assert f"{flat._num(allowance / 25.4, 4)} in ({flat._num(allowance, 2)} mm)" in page

    # With one, the pattern is laid out for it.
    steel.k_factor = Decimal("0.33")
    steel.save()
    page = admin_client.get(f"/files/{doc.pk}/flat/").content.decode()
    assert "0.33, from" in page and f"{flat._num(_length(0.33) / 25.4, 3)} × 1.969 in" in page
    assert "Made of" not in page  # only one to choose from

    # Two materials this thickness: the part says which it's made of.
    stainless = Material.objects.create(
        name="Stainless", category="metal", thickness="0.1875", k_factor=Decimal("0.45")
    )
    page = admin_client.get(f"/files/{doc.pk}/flat/").content.decode()
    assert "Made of" in page
    response = admin_client.post(f"/files/{doc.pk}/flat/", {"material": str(stainless.pk)})
    assert response.status_code == 302
    doc.refresh_from_db()
    assert doc.sheet_material == stainless
    page = admin_client.get(f"/files/{doc.pk}/flat/").content.decode()
    assert "0.45, from" in page

    from trammel.core.models import AuditEvent

    assert AuditEvent.objects.filter(action="file.sheet_material").exists()

    dxf = admin_client.get(f"/files/{doc.pk}/flat/dxf/")
    assert dxf["Content-Disposition"] == 'attachment; filename="bracket flat pattern.dxf"'
    assert b"OUTER_PROFILES" in dxf.content
    picture = admin_client.get(f"/files/{doc.pk}/flat/picture/")
    assert (
        picture["Content-Type"] == "image/svg+xml"
        and "sandbox" in picture["Content-Security-Policy"]
    )


@pytest.mark.django_db
def test_only_people_who_may_see_the_file_see_its_flat_pattern(
    client, admin, customer, django_capture_on_commit_callbacks
):
    rev = _upload(
        "bracket.step", samples.bracket(), customer, admin, django_capture_on_commit_callbacks
    )
    url = f"/files/{rev.document.pk}/flat/"
    client.force_login(make_user("viewer@example.com", "customers.view", "files.view"))
    assert client.get(url).status_code == 200
    # Seeing it isn't choosing its material.
    assert client.post(url, {"material": ""}).status_code == 403
    client.force_login(make_user("nofiles@example.com", "customers.view"))
    assert client.get(url).status_code in (403, 404)


@pytest.mark.django_db
def test_a_part_that_isnt_sheet_metal_has_no_flat_pattern(
    admin_client, admin, customer, django_capture_on_commit_callbacks
):
    rev = _upload(
        "block.step", samples.block(), customer, admin, django_capture_on_commit_callbacks
    )
    assert rev.blob.thumb_state == "ready" and rev.blob.flat == ""
    assert "sheet" not in rev.blob.model
    assert admin_client.get(f"/files/{rev.document.pk}/flat/").status_code == 404


@pytest.mark.django_db
def test_a_materials_k_factor_through_the_api(admin):
    from django.test import Client

    from trammel.accounts.models import ApiKey

    issued = ApiKey.issue(admin, "Test", ["shop.view", "shop.manage_materials"])
    token = issued[1] if isinstance(issued, tuple) else issued
    api = Client(HTTP_AUTHORIZATION=f"Bearer {token}")
    body = {"name": "Mild steel", "category": "metal", "thickness": "3/16 in", "k_factor": "0.44"}
    created = api.post("/api/v1/shop/materials", body, content_type="application/json").json()
    assert Decimal(created["k_factor"]) == Decimal("0.44")
    # An update that doesn't mention it (an older script) keeps it.
    url = f"/api/v1/shop/materials/{created['id']}"
    updated = api.put(url, {"name": "Mild steel A36"}, content_type="application/json").json()
    assert updated["name"] == "Mild steel A36" and Decimal(updated["k_factor"]) == Decimal("0.44")
    refused = api.put(url, {"name": "x", "k_factor": "1.5"}, content_type="application/json")
    assert refused.status_code == 422


@pytest.mark.django_db
def test_step_files_stored_before_are_read_again_for_flat_patterns(admin):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from trammel.core.models import BackgroundTask
    from trammel.files.models import Blob

    old = Blob.objects.create(
        sha256="f" * 64, size=1, mime="model/step", kind="step", path="x", thumb_state="ready"
    )
    executor = MigrationExecutor(connection)
    module = __import__("trammel.files.migrations.0007_flat_patterns", fromlist=["queue"])
    module.queue(executor.loader.project_state().apps, None)
    old.refresh_from_db()
    assert old.thumb_state == "pending"
    assert BackgroundTask.objects.filter(name="files.thumbnail", payload={"blob": str(old.pk)})


# ---------------------------------------------------------------------------
# On paper


@pytest.mark.django_db
def test_a_flat_pattern_prints_and_downloads_as_a_pdf(
    admin_client, admin, customer, django_capture_on_commit_callbacks
):
    from trammel.core import templating

    rev = _upload(
        "bracket.step", samples.bracket(), customer, admin, django_capture_on_commit_callbacks
    )
    doc = rev.document
    page = admin_client.get(f"/files/{doc.pk}/flat/").content.decode()
    assert f'href="/files/{doc.pk}/flat/sheet/?print=1"' in page
    assert f'href="/files/{doc.pk}/flat/sheet/?format=pdf"' in page

    response = admin_client.get(f"/files/{doc.pk}/flat/sheet/?print=1")
    sheet = response.content.decode()
    assert "Flat pattern" in sheet and "data:image/svg+xml;base64," in sheet
    assert "Bend line length" in sheet and "Bend allowance" in sheet
    assert "Cut length" in sheet and "K 0.44, Trammel&#x27;s default" in sheet
    assert "print()" in sheet  # opens the print dialog
    # A printable document: its own strict policy, as quotes and travelers.
    assert "sandbox" in response["Content-Security-Policy"]
    try:
        templating.render_pdf("<p>x</p>")
    except templating.PdfUnavailable:
        pytest.skip("WeasyPrint's system libraries aren't installed here (they are in CI).")
    pdf = admin_client.get(f"/files/{doc.pk}/flat/sheet/?format=pdf")
    assert pdf["Content-Type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert "bracket flat pattern.pdf" in pdf["Content-Disposition"]


@pytest.mark.django_db
def test_a_flat_pattern_prints_on_the_traveler_unless_left_off(
    admin_client, admin, django_capture_on_commit_callbacks
):
    from trammel.core.models import AuditEvent
    from trammel.customers.models import Customer
    from trammel.jobs import services as jobs
    from trammel.jobs.models import Job
    from trammel.workorders import services as wos

    job = jobs.create_job(Job(title="Stand", customer=Customer.internal()))
    rev = _upload("leg.step", samples.bracket(), job, admin, django_capture_on_commit_callbacks)
    _upload("block.step", samples.block(), job, admin, django_capture_on_commit_callbacks)
    jobs.transition(job, "approved", actor=admin)
    wo = wos.create(job)
    traveler = admin_client.get(f"/work-orders/{wo.pk}/traveler/").content.decode()
    # On by default, for the sheet metal part only.
    assert "Flat patterns" in traveler and traveler.count('class="flat-print"') == 1
    assert "leg.step" in traveler and "Cut length" in traveler

    doc = rev.document
    page = admin_client.get(f"/files/{doc.pk}/flat/").content.decode()
    assert "Leave it off travelers" in page
    admin_client.post(f"/files/{doc.pk}/flat/", {"traveler": "0"})
    doc.refresh_from_db()
    assert doc.flat_on_traveler is False
    assert AuditEvent.objects.filter(action="file.flat_on_traveler").exists()
    traveler = admin_client.get(f"/work-orders/{wo.pk}/traveler/").content.decode()
    assert "Flat patterns" not in traveler
    # Travelers printed together carry it too.
    admin_client.post(f"/files/{doc.pk}/flat/", {"traveler": "1"})
    bundle = admin_client.get(f"/work-orders/travelers/?wo={wo.pk}").content.decode()
    assert 'class="flat-print"' in bundle
