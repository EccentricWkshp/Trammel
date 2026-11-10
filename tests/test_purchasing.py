"""Purchase orders, landed cost and receiving (FR-PUR-1..4)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from tests.conftest import make_user
from trammel.core.access import as_actor
from trammel.core.navigation import menu_for
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.purchasing import services as purchasing
from trammel.purchasing.models import POLine, PurchaseOrder, VendorItem
from trammel.shop.models import Material, Vendor
from trammel.stock import services as stock
from trammel.stock.models import Location, StockLot
from trammel.workorders import services as wos
from trammel.workorders.models import MaterialUsage

D = Decimal


@pytest.fixture
def shelf(setup_done):
    return stock.save_location(Location(name="Shelf"))


@pytest.fixture
def vendor(setup_done):
    return Vendor.objects.create(name="Edensaw")


@pytest.fixture
def ply(shelf, vendor):
    m = Material.objects.create(name="Birch ply", unit="sheet", unit_cost=D("40"), vendor=vendor)
    stock.set_tracking(
        m, tracked=True, min_quantity=D("5"), reorder_quantity=D("10"), default_location=shelf
    )
    return m


@pytest.fixture
def screws(shelf, vendor):
    m = Material.objects.create(name="Screws", unit="each", unit_cost=D("0.05"), vendor=vendor)
    stock.set_tracking(m, tracked=True, default_location=shelf)
    return m


def _order(vendor, *lines, **charges):
    po = purchasing.create_order(vendor, **charges)
    for material, qty, price, *rest in lines:
        purchasing.save_line(
            POLine(
                order=po,
                material=material,
                description="" if material else rest[0],
                quantity=D(qty),
                unit=material.unit if material else "each",
                unit_price=D(price),
            )
        )
    return po


def test_allocate_spreads_cents_exactly():
    assert purchasing.allocate(D("10.00"), [D("1"), D("1"), D("1")]) == [
        D("3.34"),
        D("3.33"),
        D("3.33"),
    ]
    shares = purchasing.allocate(D("12.37"), [D("10.00"), D("20.00"), D("30.01")])
    assert sum(shares) == D("12.37") and shares[2] > shares[1] > shares[0]
    assert sum(purchasing.allocate(D("-5.00"), [D("3"), D("7")])) == D("-5.00")
    assert purchasing.allocate(D("1.00"), [D("0"), D("0")]) == [D("0.50"), D("0.50")]


@pytest.mark.django_db
def test_landed_cost_and_placing_the_order(vendor, ply, screws, admin):
    po = _order(
        vendor,
        (ply, "3", "38.50"),
        (screws, "200", "0.04"),
        tax=D("10.37"),
        shipping=D("25"),
        discount=D("5"),
    )
    assert po.number == "" and str(po) == "Draft order to Edensaw"
    landed = purchasing.landed(po)
    totals = purchasing.totals(po)
    assert sum(landed.values()) == totals["total"] == D("153.87")
    purchasing.place(po, actor=admin)
    po.refresh_from_db()
    assert po.number.startswith("PO-") and po.status == "ordered" and po.ordered_on
    item = VendorItem.objects.get(vendor=vendor, material=ply)
    assert item.last_price == D("38.50")
    # Next time the price starts from what was paid.
    assert purchasing.suggest(vendor, ply)["unit_price"] == D("38.50")


@pytest.mark.django_db
def test_partial_deliveries_land_the_line_exactly(vendor, ply, admin):
    po = _order(vendor, (ply, "3", "40.00"), shipping=D("10.00"))
    purchasing.place(po, actor=admin)
    line = po.lines.get()
    values = [purchasing.receive(line, quantity=D("1"), user=admin).value for _ in range(3)]
    assert values == [D("43.33"), D("43.33"), D("43.34")] and sum(values) == D("130.00")
    ply.refresh_from_db()
    assert ply.unit_cost == D("43.3400")  # quotes use the latest landed cost
    po.refresh_from_db()
    assert po.status == "closed"  # everything arrived
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_charges_lock_and_over_delivery_is_refused(vendor, ply, admin):
    po = _order(vendor, (ply, "2", "40.00"))
    purchasing.place(po, actor=admin)
    purchasing.update_order(po, shipping=D("8.00"))  # still open: the bill came late
    line = po.lines.get()
    purchasing.receive(line, quantity=D("1"), user=admin)
    with pytest.raises(ValidationError, match="fixed"):
        purchasing.update_order(po, tax=D("1.00"))
    with pytest.raises(ValidationError, match="still to come"):
        purchasing.receive(line, quantity=D("2"), user=admin)


@pytest.mark.django_db
def test_undo_a_receipt_only_while_its_stock_is_untouched(vendor, ply, admin):
    po = _order(vendor, (ply, "2", "45.00"))
    purchasing.place(po, actor=admin)
    line = po.lines.get()
    receipt = purchasing.receive(line, quantity=D("2"), user=admin)
    ply.refresh_from_db()
    assert ply.unit_cost == D("45.0000")
    purchasing.unreceive(receipt, user=admin)
    ply.refresh_from_db()
    po.refresh_from_db()
    assert ply.unit_cost == D("40.0000") and po.status == "ordered"
    assert stock.on_hand(stock.item_for(ply)) == 0
    with pytest.raises(ValidationError, match="already been undone"):
        purchasing.unreceive(receipt)
    again = purchasing.receive(line, quantity=D("2"), user=admin)
    stock.scrap(again.lot, quantity=D("1"), note="Water damage")
    with pytest.raises(ValidationError, match="untouched"):
        purchasing.unreceive(again)
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_bought_for_a_job_goes_onto_the_work_order(vendor, ply, admin):
    job = jobs.create_job(Job(title="Desk", customer=Customer.internal()))
    jobs.transition(job, "approved", actor=admin)
    wo = wos.create(job, title="Desk")
    po = purchasing.create_order(vendor, shipping=D("6.00"))
    for line in (
        POLine(
            order=po,
            material=ply,
            description="",
            quantity=D("1"),
            unit="sheet",
            unit_price=D("50.00"),
            work_order=wo,
        ),
        POLine(
            order=po,
            description="Desk hardware kit",
            quantity=D("1"),
            unit="each",
            unit_price=D("10.00"),
            work_order=wo,
        ),
    ):
        purchasing.save_line(line)
    purchasing.place(po, actor=admin)
    for line in po.lines.all():
        purchasing.receive(line, quantity=line.quantity, user=admin)
    usages = {u.description: u for u in MaterialUsage.objects.filter(work_order=wo)}
    assert usages[str(ply)].total_cost == D("55.00") and usages[str(ply)].source == "stock"
    assert usages["Desk hardware kit"].total_cost == D("11.00")
    assert stock.on_hand(stock.item_for(ply)) == 0
    assert wo.notes.filter(text__startswith="Arrived:").count() == 2
    with pytest.raises(ValidationError, match="Return it there first"):
        purchasing.unreceive(po.receipts.first())


@pytest.mark.django_db
def test_order_what_is_low(admin_client, admin, vendor, ply, screws):
    loose = Material.objects.create(name="Glue", unit="l", unit_cost=D("9"))
    stock.set_tracking(
        loose, tracked=True, min_quantity=D("2"), default_location=ply.stock.default_location
    )
    page = admin_client.get("/stock/restock/").content.decode()
    assert "Create purchase orders" in page and "Birch ply" in page
    response = admin_client.post(
        "/purchasing/restock/", {"item": [str(ply.stock.pk), str(loose.stock.pk)]}, follow=True
    )
    body = response.content.decode()
    assert "No vendor set, so not ordered: Glue" in body
    po = PurchaseOrder.objects.get()
    assert [(line.material, line.quantity) for line in po.lines.all()] == [(ply, D("10"))]
    purchasing.place(po, actor=admin)
    assert purchasing.on_order([ply.stock.pk]) == {ply.stock.pk: D("10")}
    assert "On order" in admin_client.get("/stock/restock/").content.decode()


@pytest.mark.django_db
def test_purchasing_pages(admin_client, admin, vendor, ply, shelf):
    assert admin_client.get("/purchasing/").status_code == 200
    response = admin_client.post(
        "/purchasing/new/", {"vendor": str(vendor.pk), "tax": "0", "shipping": "5", "discount": "0"}
    )
    po = PurchaseOrder.objects.get()
    assert response.url == po.get_absolute_url()
    # A blank price starts from the material's cost.
    admin_client.post(
        f"/purchasing/{po.pk}/lines/new/",
        {
            "line-material": str(ply.pk),
            "line-quantity": "2",
            "line-unit": "sheet",
            "line-update_cost": "on",
        },
    )
    line = po.lines.get()
    assert line.unit_price == D("40.0000") and line.description == "Birch ply"
    admin_client.post(f"/purchasing/{po.pk}/place/")
    po.refresh_from_db()
    page = admin_client.get(po.get_absolute_url()).content.decode()
    assert po.number in page and "Receive a delivery" in page
    admin_client.post(
        f"/purchasing/{po.pk}/receive/",
        {f"qty-{line.pk}": "2", f"loc-{line.pk}": str(shelf.pk), "received_on": "2026-11-05"},
    )
    lot = StockLot.objects.get()
    assert lot.received_on == dt.date(2026, 11, 5) and lot.value_on_hand == D("85.00")
    printed = admin_client.get(f"/purchasing/{po.pk}/document/?print=1").content.decode()
    assert "Purchase order" in printed and po.number in printed and "print()" in printed
    material_page = admin_client.get(f"/shop/materials/{ply.pk}/").content.decode()
    assert "Edensaw" in material_page and "Last price" in material_page
    settings_menu = [i.label for i in menu_for(as_actor(admin), "admin")]
    assert "Vendors" not in settings_menu  # vendors live under Purchasing now


@pytest.mark.django_db
def test_work_order_shows_what_was_bought_for_it(admin_client, admin, vendor, ply):
    job = jobs.create_job(Job(title="Shelf", customer=Customer.internal()))
    jobs.transition(job, "approved", actor=admin)
    wo = wos.create(job, title="Shelf")
    po = purchasing.create_order(vendor, expected_on=dt.date(2026, 11, 20))
    purchasing.save_line(
        POLine(
            order=po, material=ply, quantity=D("1"), unit="sheet", unit_price=D("40"), work_order=wo
        )
    )
    purchasing.place(po, actor=admin)
    page = admin_client.get(f"{wo.get_absolute_url()}?tab=purchases").content.decode()
    assert "Waiting on 1 purchase" in page and po.number in page
    home = admin_client.get("/").content.decode()
    assert "Deliveries expected" in home and po.number in home


@pytest.mark.django_db
def test_receiving_needs_its_permission(client, vendor, ply, admin):
    po = _order(vendor, (ply, "1", "40"))
    purchasing.place(po, actor=admin)
    client.force_login(make_user("buyer@example.com", "purchasing.view", "purchasing.create"))
    page = client.get(po.get_absolute_url()).content.decode()
    assert "Receive a delivery" not in page
    line = po.lines.get()
    assert client.post(f"/purchasing/{po.pk}/receive/", {f"qty-{line.pk}": "1"}).status_code == 403


@pytest.mark.django_db
def test_tax_exempt_lines_take_no_tax(vendor, ply, screws, admin):
    po = _order(
        vendor, (ply, "1", "60.00"), (screws, "400", "0.10"), tax=D("6.00"), shipping=D("10.00")
    )
    resale = po.lines.get(material=screws)
    resale.tax_exempt = True
    purchasing.save_line(resale)
    landed = purchasing.landed(po)
    # All the tax on the plywood; shipping by value over both.
    assert landed[po.lines.get(material=ply).pk] == D("60.00") + D("6.00") + D("6.00")
    assert landed[resale.pk] == D("40.00") + D("4.00")
    assert sum(landed.values()) == purchasing.totals(po)["total"]
    ply_line = po.lines.get(material=ply)
    ply_line.tax_exempt = True
    purchasing.save_line(ply_line)
    from trammel.core.lifecycle import TransitionError

    with pytest.raises(TransitionError, match="every line is tax exempt"):
        purchasing.place(po, actor=admin)


@pytest.mark.django_db
def test_a_tax_exempt_order_has_no_tax(admin_client, vendor, ply):
    from trammel.core.models import BusinessProfile

    BusinessProfile.objects.filter(pk=1).update(resale_certificate_number="WA-RS-1188")
    po = purchasing.create_order(vendor, tax_exempt=True)
    with pytest.raises(ValidationError, match="tax exempt"):
        purchasing.update_order(po, tax=D("3.00"))
    purchasing.save_line(
        POLine(order=po, material=ply, quantity=D("1"), unit="sheet", unit_price=D("40"))
    )
    printed = admin_client.get(f"/purchasing/{po.pk}/document/").content.decode()
    assert "Tax exempt" in printed and "WA-RS-1188" in printed


@pytest.mark.django_db
def test_expected_deliveries_are_on_the_calendar(admin_client, client, vendor, ply, admin):
    day = dt.date.today().replace(day=15)
    po = _order(vendor, (ply, "1", "40"))
    purchasing.update_order(po, expected_on=day)
    month = day.strftime("%Y-%m")
    assert (
        po.get_absolute_url()
        not in admin_client.get(f"/jobs/calendar/?month={month}").content.decode()
    )
    purchasing.place(po, actor=admin)
    po.refresh_from_db()
    page = admin_client.get(f"/jobs/calendar/?month={month}").content.decode()
    assert po.get_absolute_url() in page and "cal-delivery" in page and "Arriving" in page
    client.force_login(make_user("planner@example.com", "jobs.view"))
    assert (
        po.get_absolute_url() not in client.get(f"/jobs/calendar/?month={month}").content.decode()
    )
    purchasing.receive(po.lines.get(), quantity=D("1"), user=admin)
    assert (
        po.get_absolute_url()
        not in admin_client.get(f"/jobs/calendar/?month={month}").content.decode()
    )


@pytest.mark.django_db
def test_lines_are_edited_and_removed_until_a_delivery_arrives(
    admin_client, admin, vendor, ply, screws
):
    po = _order(vendor, (ply, "2", "40.00"), (screws, "100", "0.05"))
    page = admin_client.get(po.get_absolute_url()).content.decode()
    screw_line = po.lines.get(material=screws)
    assert f"/purchasing/{po.pk}/lines/{screw_line.pk}/" in page and "Remove" in page
    admin_client.post(
        f"/purchasing/{po.pk}/lines/{screw_line.pk}/",
        {
            "line-material": str(screws.pk),
            "line-quantity": "250",
            "line-unit": "each",
            "line-unit_price": "0.04",
        },
    )
    screw_line.refresh_from_db()
    assert (screw_line.quantity, screw_line.unit_price) == (D("250"), D("0.04"))
    response = admin_client.post(
        f"/purchasing/{po.pk}/lines/{screw_line.pk}/", {"delete": "1"}, follow=True
    )
    assert "Removed Screws" in response.content.decode()
    assert list(po.lines.values_list("material", flat=True)) == [ply.pk]
    purchasing.place(po, actor=admin)
    assert (
        "Remove" in admin_client.get(po.get_absolute_url()).content.decode()
    )  # nothing arrived yet
    purchasing.receive(po.lines.get(), quantity=D("1"), user=admin)
    page = admin_client.get(po.get_absolute_url()).content.decode()
    line = po.lines.get()
    assert "Lines and charges are fixed" in page
    assert f'/purchasing/{po.pk}/lines/{line.pk}/"' not in page and ">Remove<" not in page
