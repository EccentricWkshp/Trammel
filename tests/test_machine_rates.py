"""The machine rate calculator (trammel.shop.rates) and where its cost rate is used."""

from decimal import Decimal

import pytest

from trammel.core.models import ShopRates
from trammel.shop.models import Machine
from trammel.shop.rates import PRESETS, calculate

D = Decimal


def test_matches_the_pricing_calculator_sheet():
    # $8,000 laser, 50,000 h life; $500 of maintenance a year at 1,000 h a year.
    b = calculate(
        {
            "purchase_cost": D("8000"),
            "lifespan_hours": 50000,
            "maintenance_cost": D("500"),
            "maintenance_interval_hours": 1000,
        },
        D("0"),
    )
    assert [r.per_hour for r in b.rows] == [D("0.16"), D("0.5")]
    assert b.cost == D("0.66")


def test_matches_the_quoting_sheet_printer():
    # (1,600 printer + 100 service over its life) ÷ 2,000 h = 0.85/h, plus
    # 0.15 kW at 0.26/kWh, plus a 10% failure allowance.
    b = calculate(
        {
            "purchase_cost": D("1600"),
            "lifespan_hours": 2000,
            "maintenance_cost": D("100"),
            "maintenance_interval_hours": 2000,
            "power_kw": D("0.15"),
            "failure_pct": D("10"),
            "markup_pct": D("25"),
        },
        D("0.26"),
    )
    assert b.running == D("0.889")
    assert b.cost == D("0.98") and b.suggested == D("1.23")


def test_resale_wear_parts_consumables_fixed_costs_and_notes():
    b = calculate(
        {
            "purchase_cost": D("12000"),
            "salvage_value": D("2000"),
            "lifespan_hours": 10000,
            "wear_parts": [{"name": "Tube", "cost": "900", "life_hours": 2000}],
            "consumables_per_hour": D("3"),
            "annual_fixed_costs": D("1200"),
            "annual_hours": 600,
            "power_kw": D("2"),
        },
        D("0"),
    )
    assert {r.label: r.per_hour for r in b.rows} == {
        "Ownership": D("1"),
        "Tube": D("0.45"),
        "Consumables": D("3"),
        "Fixed costs": D("2"),
    }
    assert b.cost == D("6.45")
    assert any("electricity" in n for n in b.notes)  # power given, no price set


def test_empty_calculator_has_no_rows():
    b = calculate({}, D("0.15"))
    assert not b.has_inputs and b.cost == 0 and b.suggested == 0


def test_every_machine_type_has_a_preset():
    assert set(PRESETS) == set(Machine.Kind.values)


@pytest.mark.django_db
def test_cost_rate_falls_back_to_the_charge_rate(setup_done):
    plain = Machine(name="Saw", hourly_rate=D("20"))
    assert plain.calculated_cost is None and plain.cost_rate == D("20")
    costed = Machine(
        name="Laser", hourly_rate=D("40"), purchase_cost=D("8000"), lifespan_hours=8000
    )
    assert costed.cost_rate == D("1.00")


@pytest.mark.django_db
def test_machine_form_saves_wear_parts_and_can_use_the_suggested_rate(admin_client):
    rates = ShopRates.load()
    rates.electricity_rate = D("0.20")
    rates.save()
    data = {
        "name": "Epilog",
        "kind": "laser",
        "hourly_rate": "0",
        "is_active": "on",
        "purchase_cost": "15000",
        "lifespan_hours": "10000",
        "power_kw": "1.5",
        "markup_pct": "100",
        "wear_0_name": "Laser tube",
        "wear_0_cost": "900",
        "wear_0_life": "2000",
    }
    page = admin_client.get("/shop/machines/new/").content.decode()
    assert "Rate calculator" in page and "machine-presets" in page
    response = admin_client.post("/shop/machines/new/", {**data, "use_suggested": "1"})
    assert response.status_code == 302
    machine = Machine.objects.get(name="Epilog")
    assert machine.wear_parts == [{"name": "Laser tube", "cost": "900", "life_hours": 2000}]
    # 1.50 ownership + 0.45 tube + 0.30 power = 2.25 cost; 100% markup → 4.50.
    assert machine.cost_rate == D("2.25") and machine.hourly_rate == D("4.50")
    edit = admin_client.get(f"/shop/machines/{machine.pk}/").content.decode()
    assert "Laser tube" in edit and "$2.25" in edit


@pytest.mark.django_db
def test_wear_part_needs_cost_and_life(admin_client):
    response = admin_client.post(
        "/shop/machines/new/",
        {"name": "CNC", "kind": "cnc", "hourly_rate": "30", "wear_0_name": "Bits"},
    )
    assert response.status_code == 200 and "how long it lasts" in response.content.decode()
    assert not Machine.objects.filter(name="CNC").exists()


@pytest.mark.django_db
def test_machine_time_is_costed_at_the_calculated_rate(admin, customer):
    from trammel.jobs import services as jobs
    from trammel.jobs.models import Job
    from trammel.timetracking.services import _rates

    machine = Machine.objects.create(
        name="Router",
        kind="cnc",
        hourly_rate=D("45"),
        purchase_cost=D("20000"),
        lifespan_hours=10000,
    )
    jobs.create_job(Job(title="x", customer=customer))
    assert _rates("machine", machine=machine) == (D("2.00"), D("45"))
