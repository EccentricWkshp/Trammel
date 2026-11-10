"""Reports for stock, material use and purchasing."""

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.conftest import make_user
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.purchasing import services as purchasing
from trammel.purchasing.models import POLine
from trammel.reports import services as reports
from trammel.shop.models import Material, Vendor
from trammel.stock import services as stock
from trammel.stock.models import Location
from trammel.workorders import services as wos

D = Decimal
TODAY = timezone.localdate()  # the app's today, not the machine's


@pytest.fixture
def shelf(setup_done):
    return stock.save_location(Location(name="Shelf"))


@pytest.fixture
def vendor(setup_done):
    return Vendor.objects.create(name="Edensaw")


@pytest.fixture
def ply(shelf, vendor):
    m = Material.objects.create(
        name="Birch ply",
        category="sheet",
        unit="sheet",
        unit_cost=D("40"),
        vendor=vendor,
        sheet_width=D("4"),
        sheet_length=D("8"),
        sheet_dimension_unit="ft",
    )
    stock.set_tracking(m, tracked=True, default_location=shelf)
    return m


@pytest.fixture
def wo(admin):
    job = jobs.create_job(Job(title="Cabinet", customer=Customer.internal()))
    jobs.transition(job, "approved", actor=admin)
    return wos.create(job, title="Cabinet")


def _csv(response) -> str:
    return b"".join(response.streaming_content).decode()


@pytest.mark.django_db
def test_reports_page_lists_stock_and_purchasing_reports(admin_client, client):
    page = admin_client.get("/reports/").content.decode()
    for title in (
        "Material use",
        "Stock valuation",
        "Stock adjustments",
        "Remnants",
        "Purchasing spend",
        "Open orders",
        "Price history",
    ):
        assert title in page, title
    client.force_login(make_user("bookkeeper@example.com", "reports.view"))
    page = client.get("/reports/").content.decode()
    assert "Material use" in page and "Stock valuation" not in page and "Price history" not in page


@pytest.mark.django_db
def test_valuation_on_a_date_comes_from_the_ledger(admin_client, ply, wo):
    stock.receive(ply, quantity=D("3"), total_cost=D("120.00"))
    wos.record_material(wo, material=ply, quantity=D("1"), unit="")
    rows, totals = stock.valuation(TODAY)
    assert [(r["key"][0], r["quantity"], r["value"]) for r in rows] == [
        ("Birch ply", D("2"), D("80.00"))
    ]
    assert totals["value"] == stock.value_on_hand(stock.item_for(ply))
    assert stock.valuation(TODAY - dt.timedelta(days=1))[0] == []
    by_place, _ = stock.valuation(TODAY, "location")
    assert by_place[0]["key"] == ["Shelf"]
    page = admin_client.get("/stock/reports/valuation/?group=category").content.decode()
    assert "Sheet goods" in page and "$80.00" in page
    assert "Birch ply,2" in _csv(admin_client.get("/stock/reports/valuation/?format=csv")).replace(
        ".0000", ""
    )


@pytest.mark.django_db
def test_adjustments_report(admin_client, ply, shelf):
    lot = stock.receive(ply, quantity=D("4"), total_cost=D("160.00"))
    stock.scrap(lot, quantity=D("1"), note="Water damage")
    stock.count(ply, location=shelf, counted=D("2"), expected=D("3"))
    summary, detail, totals = stock.adjustments(TODAY, TODAY)
    assert {r["name"]: r["value"] for r in summary} == {
        "Counted": D("-40.00"),
        "Scrapped": D("-40.00"),
    }
    assert totals == {"lost": D("-80.00"), "found": D("0"), "net": D("-80.00")}
    page = admin_client.get("/stock/reports/adjustments/").content.decode()
    assert "Water damage" in page and "80.00 lost" in page
    assert "Water damage" in _csv(admin_client.get("/stock/reports/adjustments/?format=csv"))
    piece = stock.cut_remnant(lot, length=D("24"), width=D("12"), unit="in")
    remnants = _csv(admin_client.get("/stock/remnants/?format=csv"))
    assert piece.number in remnants and "24 × 12 in" in remnants


@pytest.mark.django_db
def test_material_use_by_source(admin_client, ply, wo):
    stock.receive(ply, quantity=D("2"), total_cost=D("90.00"))
    glue = Material.objects.create(name="Glue", unit="l", unit_cost=D("9"))
    wos.record_material(wo, material=ply, quantity=D("1"), unit="")
    wos.record_material(wo, material=glue, quantity=D("2"), unit="")
    wos.record_material(
        wo, material=None, description="Hinges", quantity=D("4"), unit="each", unit_cost=D("2.50")
    )
    rows, totals = reports.material_use(TODAY, TODAY)
    assert totals == {
        "cost": D("73.00"),
        "stock": D("45.00"),
        "price_list": D("18.00"),
        "manual": D("10.00"),
    }
    by_job, _ = reports.material_use(TODAY, TODAY, "job")
    assert len(by_job) == 1 and by_job[0]["cost"] == D("73.00")
    page = admin_client.get("/reports/materials/").content.decode()
    assert "Birch ply" in page and "$45.00" in page
    assert "Hinges" in _csv(admin_client.get("/reports/materials/?format=csv"))


@pytest.mark.django_db
def test_purchasing_reports(admin_client, admin, vendor, ply):
    other = Vendor.objects.create(name="Rockler")
    first = purchasing.create_order(
        vendor, shipping=D("10.00"), tax=D("8.00"), expected_on=TODAY - dt.timedelta(days=3)
    )
    purchasing.save_line(
        POLine(order=first, material=ply, quantity=D("2"), unit="sheet", unit_price=D("40.00"))
    )
    purchasing.place(first, actor=admin)
    second = purchasing.create_order(other)
    purchasing.save_line(
        POLine(
            order=second,
            description="Router bit",
            quantity=D("1"),
            unit="each",
            unit_price=D("35.00"),
        )
    )
    purchasing.place(second, actor=admin)
    rows, totals = purchasing.spend(TODAY.replace(month=1, day=1), TODAY)
    assert [(r["name"], r["total"]) for r in rows] == [
        ("Edensaw", D("98.00")),
        ("Rockler", D("35.00")),
    ]
    assert totals["tax"] == D("8.00") and totals["total"] == D("133.00")
    by_item, _ = purchasing.spend(TODAY.replace(month=1, day=1), TODAY, "material")
    assert {r["name"]: r["landed"] for r in by_item} == {
        "Birch ply": D("98.00"),
        "Router bit": D("35.00"),
    }
    purchasing.receive(first.lines.get(), quantity=D("1"), user=admin)
    open_orders = purchasing.open_orders()
    assert open_orders[0] == first and first.pk == open_orders[0].pk
    assert open_orders[0].late and open_orders[0].to_come == D("49.00")
    [line] = purchasing.price_history(material=ply)
    assert line.landed_unit == D("49.0000")
    for url in (
        "/purchasing/reports/spend/?group=month",
        "/purchasing/reports/open/",
        "/purchasing/reports/prices/",
    ):
        assert admin_client.get(url).status_code == 200, url
    assert "3 days late" in admin_client.get("/purchasing/reports/open/").content.decode()
    assert "Router bit" in _csv(admin_client.get("/purchasing/reports/prices/?format=csv"))
