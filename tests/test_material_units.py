"""A material's own units and barcodes (FR-STK-4), its page, and planned quantities."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from tests.conftest import make_user
from trammel.core import units
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.estimators import ESTIMATORS
from trammel.shop.models import Machine, Material, MaterialBarcode, MaterialUnit
from trammel.workorders import services as wos

D = Decimal


@pytest.fixture
def filament(setup_done):
    m = Material.objects.create(
        name="PLA black", category="filament", unit="spool", unit_cost=D("20")
    )
    MaterialUnit.objects.create(material=m, quantity=D("1000"), unit="g", stock_quantity=D("1"))
    return m


@pytest.mark.django_db
def test_conversions_go_through_the_stock_unit(filament):
    assert filament.per_stock_unit("kg") == D("1")
    assert filament.convert(D("250"), "g", "spool") == D("0.25")
    assert filament.convert(D("0.5"), "spool", "lb") == units.convert(D("500"), "g", "lb")
    assert filament.cost_per("g") == D("0.02")
    assert "g" in filament.convertible_units() and "each" not in filament.convertible_units()
    with pytest.raises(units.IncompatibleUnits):
        filament.cost_per("each")


@pytest.mark.django_db
def test_lumber_by_board_foot_used_by_the_linear_foot(setup_done):
    oak = Material.objects.create(name="Oak 4/4 x 6", unit="board_ft", unit_cost=D("9"))
    MaterialUnit.objects.create(
        material=oak, quantity=D("1"), unit="linear_ft", stock_quantity=D("0.5")
    )
    assert oak.cost_per("linear_ft") == D("4.5")
    assert oak.convert(D("12"), "in", "board_ft") == D("0.5")


@pytest.mark.django_db
def test_a_conversion_it_can_already_make_is_refused(filament, setup_done):
    for unit in ("kg", "spool"):
        row = MaterialUnit(material=filament, quantity=D("1"), unit=unit, stock_quantity=D("1"))
        with pytest.raises(ValidationError, match="already converts"):
            row.full_clean()
    sheet = Material.objects.create(
        name="Ply",
        unit="sheet",
        unit_cost=D("40"),
        sheet_width=D("4"),
        sheet_length=D("8"),
        sheet_dimension_unit="ft",
    )
    row = MaterialUnit(material=sheet, quantity=D("32"), unit="sq_ft", stock_quantity=D("1"))
    with pytest.raises(ValidationError, match="already converts"):
        row.full_clean()


@pytest.mark.django_db
def test_material_page_edits_units_and_barcodes(admin_client, filament):
    url = f"/shop/materials/{filament.pk}/"
    page = admin_client.get(url).content.decode()
    assert "1000 g = 1 spool" in page and "Spec sheets, photos and files" in page
    admin_client.post(f"{url}barcodes/", {"code": " 0123456789012 ", "quantity": "1"})
    barcode = MaterialBarcode.objects.get()
    assert barcode.code == "0123456789012" and barcode.unit == "spool"
    admin_client.post(f"{url}barcodes/", {"code": "0123456789012", "quantity": "1"})
    assert MaterialBarcode.objects.count() == 1
    found = admin_client.get("/search/?q=0123456789012").content.decode()
    assert "PLA black" in found
    admin_client.post(
        f"{url}conversions/", {"quantity": "1", "unit": "roll", "stock_quantity": "2"}
    )
    assert filament.conversions.filter(unit="roll").exists()
    edit = admin_client.get(f"{url}edit/")
    assert edit.status_code == 200


@pytest.mark.django_db
def test_viewers_see_the_material_page_without_edit_controls(client, filament):
    client.force_login(make_user("viewer@example.com", "shop.view"))
    page = client.get(f"/shop/materials/{filament.pk}/").content.decode()
    assert "PLA black" in page and "Add unit" not in page and "Cost</dt>" not in page
    response = client.post(f"/shop/materials/{filament.pk}/barcodes/", {"code": "1"})
    assert response.status_code == 403


@pytest.mark.django_db
def test_work_order_plans_the_estimated_quantity(filament, admin):
    printer = Machine.objects.create(name="Printer", kind="printer_3d", hourly_rate=D("5"))
    job = jobs.create_job(Job(title="Brackets", customer=Customer.internal()))
    revision = quotes.create_quote(job)
    form = ESTIMATORS["print3d"].form_class()(
        {
            "description": "Brackets",
            "quantity": "4",
            "material": str(filament.pk),
            "grams": "50",
            "print_hours": "2",
            "machine": str(printer.pk),
            "failure_pct": "0",
        }
    )
    assert form.is_valid(), form.errors
    quotes.save_estimate(quotes.estimate_line(revision, "print3d", form.cleaned_data))
    jobs.transition(job, "approved", actor=admin)
    wo = wos.create(job, revision=revision)
    [planned] = wo.planned_materials.all()
    assert planned.material == filament
    assert (planned.quantity, planned.unit) == (D("200"), "g")
