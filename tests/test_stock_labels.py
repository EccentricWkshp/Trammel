"""Labels, scanning and stock on the shop floor (FR-STK-7, -8)."""

import re
from decimal import Decimal

import pytest

from tests.conftest import make_user
from trammel.core import modules, templating
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.shop.models import Material, MaterialBarcode, MaterialUnit
from trammel.stock import labels
from trammel.stock import services as stock
from trammel.stock.models import Location, StockLot
from trammel.workorders import services as wos
from trammel.workorders.models import MaterialUsage

D = Decimal


@pytest.fixture
def shelf(setup_done):
    return stock.save_location(Location(name="Shelf 1"))


@pytest.fixture
def screws(shelf):
    m = Material.objects.create(name="Wood screw #8", unit="each", unit_cost=D("0.05"))
    MaterialUnit.objects.create(material=m, quantity=D("1"), unit="box", stock_quantity=D("100"))
    MaterialBarcode.objects.create(material=m, code="012345678905")
    stock.set_tracking(m, tracked=True, default_location=shelf)
    return m


@pytest.fixture
def lots(screws):
    return [
        stock.receive(screws, quantity=D("100"), total_cost=D("5.00")),
        stock.receive(screws, quantity=D("100"), total_cost=D("7.00")),
    ]


@pytest.fixture
def wo(admin):
    job = jobs.create_job(Job(title="Shop cabinet", customer=Customer.internal()))
    jobs.transition(job, "approved", actor=admin)
    return wos.create(job, title="Build it")


@pytest.mark.django_db
def test_label_links_open_the_record_on_the_floor(admin_client, lots, shelf, screws, wo):
    lot = lots[0]
    assert admin_client.get(f"/s/{lot.pk}/").url == f"/stock/f/lot/{lot.pk}/"
    assert admin_client.get(f"/s/{shelf.pk}/").url == f"/stock/f/location/{shelf.pk}/"
    assert admin_client.get(f"/s/{screws.pk}/").url == f"/stock/f/material/{screws.pk}/"
    assert admin_client.get(f"/s/{wo.pk}/").url == f"/floor/wo/{wo.pk}/"
    assert admin_client.get("/s/00000000-0000-0000-0000-000000000000/").status_code == 404


@pytest.mark.django_db
def test_floor_find_takes_any_code(admin_client, lots, screws):
    lot = lots[1]
    scanned = admin_client.get(f"/floor/find/?q=https://shop.example/s/{lot.pk}/")
    assert scanned.url == f"/stock/f/lot/{lot.pk}/"
    assert admin_client.get(f"/floor/find/?q={lot.number.lower()}").url.endswith(f"{lot.pk}/")
    by_barcode = admin_client.get("/floor/find/?q=012345678905")
    assert by_barcode.url == f"/stock/f/material/{screws.pk}/"
    unknown = admin_client.get("/floor/find/?q=4006381333931")
    assert unknown.url == "/stock/new/?barcode=4006381333931"
    form = admin_client.get(unknown.url).content.decode()
    assert 'value="4006381333931"' in form and "No material has the barcode" in form


@pytest.mark.django_db
def test_unknown_code_without_rights_to_add(client, lots):
    client.force_login(make_user("hand@example.com", "workorders.view", "stock.view"))
    response = client.get("/floor/find/?q=4006381333931", follow=True)
    assert "Nothing matches" in response.content.decode()


@pytest.mark.django_db
def test_adding_a_material_saves_its_barcode(admin_client):
    data = {
        "name": "Hinge",
        "category": "hardware",
        "unit": "each",
        "unit_cost": "2",
        "is_active": "on",
        "barcode": "4006381333931",
    }
    assert admin_client.post("/stock/new/", data).status_code == 302
    hinge = Material.objects.get(name="Hinge")
    assert list(hinge.barcodes.values_list("code", flat=True)) == ["4006381333931"]
    again = admin_client.post("/stock/new/", data | {"name": "Other hinge"})
    assert "already the barcode of Hinge" in again.content.decode()
    assert not Material.objects.filter(name="Other hinge").exists()


def test_sheet_positions_start_where_asked_and_wrap_to_a_new_sheet():
    found = [labels.Label(title=f"L{n}", code=str(n)) for n in range(3)]
    pages = labels.sheets(found, start=29, copies=2)
    assert [len(p) for p in pages] == [2, 4]
    first = pages[0][0]
    assert (first.top, first.left) == (D("9.5"), D("2.9375"))  # row 10, column 2
    assert (pages[1][0].top, pages[1][0].left) == (D("0.5"), D("0.1875"))
    assert [x.code for x in pages[1]] == ["1", "1", "2", "2"]


@pytest.mark.django_db
def test_print_labels_for_a_material_s_lots(admin_client, lots, screws):
    options = admin_client.get(f"/stock/labels/?lots_of={screws.pk}").content.decode()
    assert "2 labels" in options and lots[0].number in options
    sheet = admin_client.get(f"/stock/labels/print/?lots_of={screws.pk}&start=5&print=1")
    page = sheet.content.decode()
    assert sheet.status_code == 200 and page.count('class="label"') == 2
    assert "<svg" in page and "size: 8.5in 11in" in page and "print()" in page
    assert re.search(r"top: 1\.5in; left: 2\.9375in", page)  # label 5: row 2, column 2
    single = admin_client.get(f"/stock/labels/print/?lot={lots[0].pk}&layout=single")
    assert "size: 2.25in 1.25in" in single.content.decode()
    if templating.pdf_available():
        pdf = admin_client.get(f"/stock/labels/print/?lot={lots[0].pk}&format=pdf")
        assert pdf["Content-Type"] == "application/pdf"


@pytest.mark.django_db
def test_use_a_lot_on_a_work_order_from_the_floor(admin_client, lots, screws, wo):
    newer = lots[1]
    admin_client.get(f"/floor/wo/{wo.pk}/")  # the floor remembers the work order
    page = admin_client.get(f"/stock/f/lot/{newer.pk}/").content.decode()
    assert f'value="{wo.pk}" selected' in page and f"Use {newer.number}" in page
    response = admin_client.post(
        "/stock/f/use/",
        {
            "material": str(screws.pk),
            "lot": str(newer.pk),
            "work_order": str(wo.pk),
            "quantity": "10",
            "unit": "each",
            "billable": "on",
            "next": f"/stock/f/lot/{newer.pk}/",
        },
    )
    assert response.url == f"/stock/f/lot/{newer.pk}/"
    [usage] = MaterialUsage.objects.filter(work_order=wo)
    assert usage.total_cost == D("0.70")  # the newer lot's cost, not the oldest's
    newer.refresh_from_db()
    assert newer.qty_on_hand == D("90")


@pytest.mark.django_db
def test_count_a_location_from_the_floor(admin_client, lots, screws, shelf):
    page = admin_client.get(f"/stock/f/location/{shelf.pk}/").content.decode()
    assert "Wood screw #8" in page and "200 each here" in page
    response = admin_client.post(
        f"/stock/materials/{screws.pk}/count/",
        {
            "location": str(shelf.pk),
            "expected": "200",
            "unit": "each",
            "counted": "195",
            "next": f"/stock/f/location/{shelf.pk}/",
        },
    )
    assert response.url == f"/stock/f/location/{shelf.pk}/"
    assert stock.on_hand(stock.item_for(screws), shelf) == D("195")


@pytest.mark.django_db
def test_work_order_material_form_takes_a_lot_number_or_label(admin_client, lots, screws, wo):
    page = admin_client.get(f"{wo.get_absolute_url()}?tab=materials").content.decode()
    assert 'name="lot"' in page
    for lot_value in (lots[1].number, f"https://shop.example/s/{lots[1].pk}/"):
        admin_client.post(
            f"/work-orders/{wo.pk}/materials/",
            {"material": str(screws.pk), "quantity": "50", "unit": "each", "lot": lot_value},
        )
    lots[1].refresh_from_db()
    assert lots[1].qty_on_hand == D("0")  # both draws came from the lot named
    admin_client.post(
        f"/work-orders/{wo.pk}/materials/",
        {"material": str(screws.pk), "quantity": "1", "lot": "LOT-99999"},
        follow=True,
    )
    assert MaterialUsage.objects.filter(work_order=wo).count() == 2


@pytest.mark.django_db
def test_no_lot_field_while_stock_is_off(admin_client, wo):
    modules.set_enabled("stock", False)
    page = admin_client.get(f"{wo.get_absolute_url()}?tab=materials").content.decode()
    assert 'name="lot"' not in page
    assert StockLot.objects.count() == 0


@pytest.mark.django_db
def test_stock_is_in_the_shop_floor_bar(admin_client, client, lots, screws):
    home = admin_client.get("/floor/").content.decode()
    bar = home[home.index('class="floor-nav"') : home.index("</nav>")]
    assert 'href="/stock/f/"' in bar and ">Stock<" in bar
    page = admin_client.get("/stock/f/").content.decode()
    assert "Wood screw #8" in page and "Shelf 1" in page and "200 each" in page
    assert "Wood screw" in admin_client.get("/stock/f/?q=screw").content.decode()
    assert admin_client.get(f"/stock/f/?q={lots[0].number}").url == f"/stock/f/lot/{lots[0].pk}/"
    client.force_login(make_user("hand@example.com", "workorders.view"))
    assert 'href="/stock/f/"' not in client.get("/floor/").content.decode()


@pytest.mark.django_db
def test_low_stock_leads_the_floor_stock_page(admin_client, lots, screws):
    stock.set_tracking(screws, tracked=True, min_quantity=D("500"))
    page = admin_client.get("/stock/f/").content.decode()
    assert page.index("Low stock") < page.index("Materials in stock")
