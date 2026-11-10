"""Units (FR-STK-4), machines and the materials price list (FR-ADM-4, §7.6 note)."""

import json
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from tests.conftest import make_user
from trammel.core import units
from trammel.core.models import AuditEvent
from trammel.shop.models import Machine, Material


def test_conversions_are_exact():
    assert units.convert(1, "ft", "in") == 12
    assert units.convert(1, "board_ft", "cu_in") == 144
    assert units.convert(1, "kg", "g") == 1000
    assert units.convert(30, "min", "hr") == Decimal("0.5")
    assert units.canonical(2, "in") == Decimal("50.8")


def test_incompatible_units_raise():
    with pytest.raises(units.IncompatibleUnits):
        units.convert(1, "kg", "ft")
    with pytest.raises(units.IncompatibleUnits):
        units.get("furlong")


@pytest.mark.django_db
def test_material_cost_converts_within_a_dimension(setup_done):
    filament = Material(name="PLA", unit="kg", unit_cost=Decimal("24.00"))
    assert filament.cost_per("g") == Decimal("0.024")
    plank = Material(name="Walnut", unit="board_ft", unit_cost=Decimal("14.00"))
    assert plank.cost_per("cu_in") == Decimal("14") / 144


@pytest.mark.django_db
def test_sheet_cost_prices_by_area(setup_done):
    ply = Material(
        name="Birch ply",
        unit="sheet",
        unit_cost=Decimal("48.00"),
        sheet_width=Decimal("24"),
        sheet_length=Decimal("12"),
        sheet_dimension_unit="in",
    )
    assert ply.cost_per("sq_ft") == Decimal("24.00")  # a 2 ft² sheet
    no_size = Material(name="Acrylic", unit="sheet", unit_cost=Decimal("30"))
    with pytest.raises(units.IncompatibleUnits):
        no_size.cost_per("sq_in")


@pytest.mark.django_db
def test_count_units_are_not_interchangeable(setup_done):
    # A box of 100 screws at $6 must not price one screw at $6.
    screws = Material(name="Screws", unit="box", unit_cost=Decimal("6.00"))
    assert screws.cost_per("box") == Decimal("6.00")
    with pytest.raises(units.IncompatibleUnits):
        screws.cost_per("each")
    assert not units.compatible("box", "each") and units.compatible("each", "each")
    with pytest.raises(units.IncompatibleUnits):
        units.convert(1, "spool", "sheet")


@pytest.mark.django_db
def test_count_unit_mismatch_is_refused_on_a_quote_line(setup_done):
    from trammel.quotes.forms import MaterialLineForm

    screws = Material.objects.create(name="Screws", unit="box", unit_cost=Decimal("6.00"))
    form = MaterialLineForm(data={"material": screws.pk, "quantity": "50", "unit": "each"})
    assert not form.is_valid()
    assert "cannot price it per each" in form.errors["unit"][0]


@pytest.mark.django_db
def test_sheet_size_needs_both_dimensions_and_a_unit(setup_done):
    m = Material(name="Odd", unit="sheet", unit_cost=1, sheet_width=Decimal("12"))
    with pytest.raises(ValidationError):
        m.full_clean()


@pytest.mark.django_db
def test_material_markup_falls_back_to_shop_default(setup_done):
    from trammel.core.models import ShopRates

    rates = ShopRates.load()
    rates.default_material_markup = Decimal("20")
    m = Material(name="Bolt", unit="each", unit_cost=Decimal("0.50"))
    assert m.effective_markup(rates) == Decimal("20")
    m.markup = Decimal("50")
    assert m.effective_markup(rates) == Decimal("50")


@pytest.mark.django_db
def test_machine_screens_and_audit(admin_client):
    response = admin_client.post(
        "/shop/machines/new/",
        {"name": "Laser 1", "kind": "laser", "hourly_rate": "45", "is_active": "on"},
    )
    assert response.status_code == 302
    machine = Machine.objects.get()
    assert AuditEvent.objects.filter(action="create", entity_id=str(machine.pk)).exists()
    assert "Laser 1" in admin_client.get("/shop/machines/").content.decode()
    assert admin_client.get(f"/shop/machines/{machine.pk}/").status_code == 200


@pytest.mark.django_db
def test_view_only_users_cannot_edit(client, setup_done):
    viewer = make_user("viewer@example.com", "shop.view")
    client.force_login(viewer)
    assert client.get("/shop/materials/").status_code == 200
    assert client.get("/shop/materials/new/").status_code == 403


@pytest.mark.django_db
def test_material_api(admin_client):
    created = admin_client.post(
        "/api/v1/shop/materials",
        json.dumps({"name": "PETG", "category": "filament", "unit": "kg", "unit_cost": "22.5"}),
        content_type="application/json",
    )
    assert created.status_code == 201
    assert admin_client.get("/api/v1/shop/materials").json()[0]["name"] == "PETG"
