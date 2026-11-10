"""Stock: lots, the ledger, FIFO consumption at lot cost (FR-STK-1..3, -8)."""

import threading
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction

from tests.conftest import make_user
from trammel.core import modules
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.shop.models import Material, MaterialUnit
from trammel.stock import services as stock
from trammel.stock.models import Location, StockLot, StockMove, StockSettings
from trammel.workorders import services as wos
from trammel.workorders.models import MaterialUsage

D = Decimal


@pytest.fixture
def shelf(setup_done):
    rack = stock.save_location(Location(name="Rack A"))
    return stock.save_location(Location(name="Shelf 1", parent=rack))


@pytest.fixture
def bin2(shelf):
    return stock.save_location(Location(name="Bin 2", parent=shelf.parent))


@pytest.fixture
def screws(shelf):
    """Screws counted each, bought by the box of 100, tracked in stock."""
    m = Material.objects.create(
        name="Wood screw #8", category="hardware", unit="each", unit_cost=D("0.05")
    )
    MaterialUnit.objects.create(material=m, quantity=D("1"), unit="box", stock_quantity=D("100"))
    stock.set_tracking(m, tracked=True, min_quantity=D("50"), default_location=shelf)
    return m


@pytest.fixture
def wo(admin):
    job = jobs.create_job(Job(title="Shop cabinet", customer=Customer.internal()))
    jobs.transition(job, "approved", actor=admin)
    return wos.create(job, title="Build it")


def _use(wo, material, qty, unit="", **kw):
    return wos.record_material(wo, material=material, quantity=D(qty), unit=unit, **kw)


@pytest.mark.django_db
def test_locations_nest_and_keep_their_path(shelf):
    assert shelf.path == "Rack A / Shelf 1"
    rack = shelf.parent
    rack.name = "Rack B"
    stock.save_location(rack)
    shelf.refresh_from_db()
    assert shelf.path == "Rack B / Shelf 1"
    rack.parent = shelf
    with pytest.raises(ValidationError, match="inside itself"):
        stock.save_location(rack)


@pytest.mark.django_db
def test_receive_creates_a_lot_and_sets_the_latest_cost(screws, shelf, admin):
    lot = stock.receive(screws, quantity=D("2"), unit="box", total_cost=D("12.00"), user=admin)
    assert lot.number.startswith("LOT-") and lot.location == shelf
    assert lot.qty_on_hand == D("200") and lot.value_on_hand == D("12.00")
    screws.refresh_from_db()
    assert screws.unit_cost == D("0.0600")  # quotes now use what was last paid
    opening = stock.receive(screws, quantity=D("10"), unit_cost=D("1"), opening=True)
    screws.refresh_from_db()
    assert opening.origin == "opening" and screws.unit_cost == D("0.0600")
    [move] = opening.moves.all()
    assert move.reason == "opening" and move.quantity == D("10")


@pytest.mark.django_db
def test_untracked_materials_stay_on_the_price_list(setup_done, wo, admin):
    ply = Material.objects.create(name="Ply", unit="sheet", unit_cost=D("40"))
    with pytest.raises(ValidationError, match="not tracked"):
        stock.receive(ply, quantity=D("1"), unit_cost=D("40"))
    [usage] = _use(wo, ply, "2", user=admin)
    assert usage.source == MaterialUsage.Source.PRICE_LIST and usage.total_cost == D("80.00")
    [typed] = wos.record_material(
        wo, material=None, description="Glue", quantity=D("1"), unit="each", unit_cost=D("4")
    )
    assert typed.source == MaterialUsage.Source.MANUAL


@pytest.mark.django_db
def test_consumption_is_first_in_first_out_one_usage_per_lot(screws, wo, admin):
    import datetime as dt

    old = stock.receive(
        screws, quantity=D("100"), total_cost=D("5.00"), received_on=dt.date(2026, 1, 5)
    )
    new = stock.receive(
        screws, quantity=D("100"), total_cost=D("8.00"), received_on=dt.date(2026, 3, 1)
    )
    usages = _use(wo, screws, "1.5", "box", user=admin)  # 150 each
    assert [u.source for u in usages] == ["stock", "stock"]
    assert [u.quantity for u in usages] == [D("1"), D("0.5")]  # in the unit entered
    assert [u.total_cost for u in usages] == [D("5.00"), D("4.00")]
    old.refresh_from_db()
    new.refresh_from_db()
    assert (old.qty_on_hand, old.value_on_hand) == (D("0"), D("0"))
    assert (new.qty_on_hand, new.value_on_hand) == (D("50"), D("4.00"))
    moves = StockMove.objects.filter(reason="consume").order_by("created_at")
    assert [m.source_id for m in moves] == [u.pk for u in usages]
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_lot_value_reaches_exactly_zero_after_uneven_draws(screws, wo, admin):
    lot = stock.receive(screws, quantity=D("3"), total_cost=D("10.00"))
    costs = [_use(wo, screws, "1", user=admin)[0].total_cost for _ in range(3)]
    assert costs == [D("3.33"), D("3.34"), D("3.33")] and sum(costs) == D("10.00")
    lot.refresh_from_db()
    assert lot.qty_on_hand == 0 and lot.value_on_hand == D("0.00")
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_a_lot_can_be_picked(screws, wo, admin):
    first = stock.receive(screws, quantity=D("10"), total_cost=D("1.00"))
    second = stock.receive(screws, quantity=D("10"), total_cost=D("2.00"))
    [usage] = _use(wo, screws, "4", lot=second.pk, user=admin)
    assert usage.total_cost == D("0.80")
    first.refresh_from_db()
    assert first.qty_on_hand == D("10")
    with pytest.raises(ValidationError, match="has only"):
        _use(wo, screws, "7", lot=second.pk, user=admin)


@pytest.mark.django_db
def test_tracked_material_takes_no_typed_cost(screws, wo, admin):
    stock.receive(screws, quantity=D("10"), total_cost=D("1.00"))
    with pytest.raises(ValidationError, match="Leave the cost blank"):
        _use(wo, screws, "1", unit_cost=D("9"), user=admin)


@pytest.mark.django_db
def test_shortfall_is_refused_or_recorded_as_found_never_at_zero(screws, wo, admin):
    stock.receive(screws, quantity=D("10"), total_cost=D("1.00"))
    with pytest.raises(ValidationError, match="Only 10 each"):
        _use(wo, screws, "15", user=admin)
    settings = StockSettings.load()
    settings.shortfall = "found"
    settings.save()
    usages = _use(wo, screws, "15", user=admin)
    assert sum(u.total_cost for u in usages) == D("1.50")  # 5 found at the current 0.10
    found = StockLot.objects.get(origin="adjust")
    assert found.unit_cost == D("0.1000") and found.qty_on_hand == 0
    # With no cost known anywhere, found stock is refused rather than valued at nothing.
    free = Material.objects.create(name="Offcut", unit="each", unit_cost=D("0"))
    stock.set_tracking(free, tracked=True, default_location=found.location)
    with pytest.raises(ValidationError, match="no cost yet"):
        _use(wo, free, "1", user=admin)


@pytest.mark.django_db
def test_returns_go_back_to_their_lot_at_the_cost_they_left_at(screws, wo, admin):
    lot = stock.receive(screws, quantity=D("10"), total_cost=D("3.00"))
    _use(wo, screws, "4", user=admin)
    [back] = _use(wo, screws, "-1", user=admin)
    assert back.quantity == D("-1") and back.total_cost == D("-0.30")
    lot.refresh_from_db()
    assert lot.qty_on_hand == D("7") and lot.value_on_hand == D("2.10")
    with pytest.raises(ValidationError, match="no more than that"):
        _use(wo, screws, "-4", user=admin)
    _use(wo, screws, "-3", user=admin)
    lot.refresh_from_db()
    assert lot.qty_on_hand == D("10") and lot.value_on_hand == D("3.00")
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_transfer_moves_a_whole_lot_or_splits_part_off(screws, shelf, bin2, admin):
    lot = stock.receive(screws, quantity=D("10"), total_cost=D("1.00"))
    part = stock.transfer(lot, quantity=D("4"), to=bin2, user=admin)
    lot.refresh_from_db()
    assert part.pk != lot.pk and part.parent == lot and part.received_on == lot.received_on
    assert (part.qty_on_hand, part.value_on_hand) == (D("4"), D("0.40"))
    assert (lot.qty_on_hand, lot.value_on_hand) == (D("6"), D("0.60"))
    whole = stock.transfer(lot, quantity=D("6"), to=bin2, user=admin)
    assert whole.pk == lot.pk and whole.location == bin2
    with pytest.raises(ValidationError, match="already in"):
        stock.transfer(lot, quantity=D("1"), to=bin2)
    assert stock.on_hand(stock.item_for(screws), bin2) == D("10")
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_count_sets_the_quantity_and_refuses_a_stale_count(screws, shelf, admin):
    stock.receive(screws, quantity=D("100"), total_cost=D("5.00"))
    assert stock.count(screws, location=shelf, counted=D("90"), expected=D("100")) == D("-10")
    assert stock.on_hand(stock.item_for(screws)) == D("90")
    with pytest.raises(ValidationError, match="changed while you were counting"):
        stock.count(screws, location=shelf, counted=D("80"), expected=D("100"))
    assert stock.count(screws, location=shelf, counted=D("1"), unit="box", expected=D("90")) == 10
    found = StockLot.objects.filter(origin="count").get()
    assert found.value_on_hand == D("0.50")  # at the newest lot's cost
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_scrap_needs_a_reason(screws, admin):
    lot = stock.receive(screws, quantity=D("10"), total_cost=D("1.00"))
    with pytest.raises(ValidationError, match="why"):
        stock.scrap(lot, quantity=D("1"), note="")
    stock.scrap(lot, quantity=D("2"), note="Stripped heads", user=admin)
    lot.refresh_from_db()
    assert lot.qty_on_hand == D("8") and lot.value_on_hand == D("0.80")


@pytest.mark.django_db
def test_ledger_is_append_only_at_the_database(screws):
    stock.receive(screws, quantity=D("10"), total_cost=D("1.00"))
    move = StockMove.objects.get()
    with pytest.raises(ValidationError):
        move.save()
    for sql in (
        "UPDATE stock_stockmove SET quantity = 0",
        "DELETE FROM stock_stockmove",
    ):
        with (
            pytest.raises(DatabaseError, match="append-only"),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(sql)
    assert StockMove.objects.get().quantity == D("10")


@pytest.mark.django_db
def test_stop_tracking_only_when_empty_and_stock_stays_on_while_tracked(screws, wo, admin):
    stock.receive(screws, quantity=D("10"), total_cost=D("1.00"))
    with pytest.raises(ValidationError, match="still in stock"):
        stock.set_tracking(screws, tracked=False)
    modules.set_enabled("purchasing", False)  # it depends on stock
    with pytest.raises(ValidationError, match="tracked in stock"):
        modules.set_enabled("stock", False)
    _use(wo, screws, "10", user=admin)
    stock.set_tracking(screws, tracked=False)
    modules.set_enabled("stock", False)
    assert not modules.is_enabled("stock")


@pytest.mark.django_db
def test_restock_list_and_low_stock_on_home(admin_client, screws):
    stock.receive(screws, quantity=D("40"), total_cost=D("2.00"))
    assert list(stock.restock_list()) == [stock.item_for(screws)]
    page = admin_client.get("/").content.decode()
    section = page[page.index("Low stock") :]
    assert "Wood screw #8" in section[: section.index("</section>")]
    csv = b"".join(admin_client.get("/stock/restock/?format=csv").streaming_content).decode()
    assert "Wood screw #8" in csv


@pytest.mark.django_db(transaction=True)
def test_two_people_drawing_the_last_unit(setup_done):
    """One draw succeeds; the other waits for the first and is then refused."""
    shelf = stock.save_location(Location(name="Shelf"))
    m = Material.objects.create(name="Hinge", unit="each", unit_cost=D("3"))
    stock.set_tracking(m, tracked=True, default_location=shelf)
    stock.receive(m, quantity=D("1"), total_cost=D("3.00"))
    job = jobs.create_job(Job(title="Doors", customer=Customer.internal()))
    from trammel.core.access import SYSTEM

    jobs.transition(job, "approved", actor=SYSTEM)
    wo = wos.create(job, title="Doors")
    results, start = [], threading.Barrier(2)

    def draw():
        try:
            start.wait()
            wos.record_material(wo, material=m, quantity=D("1"), unit="")
            results.append("ok")
        except ValidationError:
            results.append("refused")
        finally:
            connection.close()

    threads = [threading.Thread(target=draw) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok", "refused"]
    assert MaterialUsage.objects.filter(material=m).count() == 1
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_material_page_stock_tab_and_actions(admin_client, screws, shelf, bin2):
    url = f"/shop/materials/{screws.pk}/"
    page = admin_client.get(url + "?tab=stock").content.decode()
    assert "Receive stock" in page and "None in stock." in page
    response = admin_client.post(
        f"/stock/materials/{screws.pk}/receive/",
        {
            "kind": "receive",
            "quantity": "1",
            "unit": "box",
            "total_cost": "6.00",
            "location": str(shelf.pk),
            "received_on": "2026-11-01",
            "update_cost": "on",
        },
    )
    assert response.status_code == 302
    lot = StockLot.objects.get()
    assert lot.qty_on_hand == D("100")
    page = admin_client.get(url + "?tab=stock").content.decode()
    assert lot.number in page and "Rack A / Shelf 1" in page
    admin_client.post(
        f"/stock/lots/{lot.pk}/transfer/", {"quantity": "30", "unit": "each", "to": str(bin2.pk)}
    )
    assert stock.on_hand(stock.item_for(screws), bin2) == D("30")
    admin_client.post(
        f"/stock/materials/{screws.pk}/count/",
        {"location": str(shelf.pk), "counted": "65", "unit": "each", "expected": "70"},
    )
    assert stock.on_hand(stock.item_for(screws), shelf) == D("65")
    for path in ("/stock/", "/stock/moves/", "/stock/locations/", f"/stock/lots/{lot.pk}/"):
        assert admin_client.get(path).status_code == 200, path
    assert admin_client.get(f"/stock/locations/{shelf.pk}/").status_code == 200
    csv = b"".join(admin_client.get("/stock/valuation.csv").streaming_content).decode()
    assert lot.number in csv


@pytest.mark.django_db
def test_stock_viewer_sees_no_costs_and_cannot_act(client, screws):
    stock.receive(screws, quantity=D("10"), total_cost=D("1.00"))
    user = make_user("floor@example.com", "stock.view", "shop.view")
    client.force_login(user)
    page = client.get("/stock/").content.decode()
    assert "Wood screw #8" in page and "Export valuation" not in page
    assert client.get("/stock/valuation.csv").status_code == 404
    lot = StockLot.objects.get()
    response = client.post(f"/stock/lots/{lot.pk}/scrap/", {"quantity": "1", "note": "x"})
    assert response.status_code == 403
    tab = client.get(f"/shop/materials/{screws.pk}/?tab=stock").content.decode()
    assert "Receive stock" not in tab and "$" not in tab.split('class="tabs"')[1]


@pytest.mark.django_db
def test_api_receive_lots_and_use_a_lot(client, admin, screws, shelf, wo):
    from trammel.accounts.models import ApiKey
    from trammel.core import access

    scopes = sorted(access.permissions_of(access.as_actor(admin)))
    _key, raw = ApiKey.issue(admin, "Test client", scopes)
    auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
    response = client.post(
        "/api/v1/stock/receive",
        {"material": str(screws.pk), "quantity": "2", "unit": "box", "total_cost": "10.00"},
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 201, response.content
    lot_id = response.json()["id"]
    lots = client.get(f"/api/v1/stock/lots?material={screws.pk}", **auth).json()
    assert [x["qty_on_hand"] for x in lots] == ["200.0000"]
    response = client.post(
        f"/api/v1/work-orders/{wo.pk}/materials",
        {"material": str(screws.pk), "quantity": "20", "lot": lot_id},
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 201, response.content
    assert response.json()["total_cost"] == "1.00"
    response = client.post(
        "/api/v1/stock/count",
        {
            "material": str(screws.pk),
            "location": str(shelf.pk),
            "counted": "170",
            "expected": "100",
        },
        content_type="application/json",
        **auth,
    )
    assert response.status_code == 422


MATERIAL = {
    "name": "Brass hinge 2 in",
    "category": "hardware",
    "unit": "each",
    "unit_cost": "1.25",
    "is_active": "on",
}


@pytest.mark.django_db
def test_add_a_material_and_keep_it_in_stock_in_one_form(admin_client):
    page = admin_client.get("/stock/").content.decode()
    assert "Add material" in page and "Add stock item" not in page
    assert admin_client.get("/shop/materials/new/").url == "/stock/new/"
    form = admin_client.get("/stock/new/").content.decode()
    assert 'name="stock-keep"' in form and "checked" in form
    response = admin_client.post(
        "/stock/new/",
        MATERIAL
        | {
            "stock-keep": "on",
            "stock-new_location": "Hardware drawer",
            "stock-min_quantity": "10",
            "stock-on_hand": "24",
        },
    )
    assert response.status_code == 302
    hinge = Material.objects.get(name="Brass hinge 2 in")
    assert response.url == f"/shop/materials/{hinge.pk}/?tab=stock"
    item = stock.item_for(hinge)
    assert item.tracked and item.min_quantity == D("10")
    assert item.default_location.path == "Hardware drawer"
    lot = StockLot.objects.get(item=item)
    assert (lot.qty_on_hand, lot.value_on_hand, lot.origin) == (D("24"), D("30.00"), "opening")


@pytest.mark.django_db
def test_a_material_for_one_job_skips_stock(admin_client):
    page = admin_client.post("/stock/new/", MATERIAL | {"stock-keep": "on"})
    assert page.status_code == 200 and "Say where it is kept." in page.content.decode()
    assert not Material.objects.exists()  # nothing saved while the form has errors
    response = admin_client.post("/stock/new/", MATERIAL)  # “Keep it in stock” unticked
    hinge = Material.objects.get()
    assert response.url == f"/shop/materials/{hinge.pk}/" and stock.item_for(hinge) is None


@pytest.mark.django_db
def test_keep_an_existing_material_in_stock(admin_client, shelf):
    walnut = Material.objects.create(name="Walnut", unit="board_ft", unit_cost=D("14"))
    listing = admin_client.get("/stock/").content.decode()
    assert f"/stock/track/{walnut.pk}/" in listing
    page = admin_client.get(f"/stock/track/{walnut.pk}/").content.decode()
    assert "Keep Walnut in stock" in page and 'name="stock-keep"' not in page
    response = admin_client.post(
        f"/stock/track/{walnut.pk}/",
        {"stock-location": str(shelf.pk), "stock-on_hand": "40", "stock-on_hand_cost": "12.50"},
    )
    assert response.status_code == 302
    assert stock.value_on_hand(stock.item_for(walnut)) == D("500.00")
    walnut.refresh_from_db()
    assert walnut.unit_cost == D("14")  # an opening balance leaves the cost alone
    # Already tracked: the page sends you to its stock tab.
    assert admin_client.get(f"/stock/track/{walnut.pk}/").url.endswith("?tab=stock")


@pytest.mark.django_db
def test_add_material_without_stock_rights_has_no_stock_section(client, setup_done):
    client.force_login(make_user("buyer@example.com", "shop.view", "shop.manage_materials"))
    page = client.get("/stock/new/").content.decode()
    assert 'name="name"' in page and 'name="stock-keep"' not in page
    response = client.post("/stock/new/", MATERIAL)
    assert response.status_code == 302 and Material.objects.filter(name=MATERIAL["name"]).exists()


@pytest.mark.django_db
def test_materials_live_under_stock_and_return_to_the_menu_without_it(admin_client, admin, screws):
    plain = Material.objects.create(name="Danish oil", unit="l", unit_cost=D("38"))
    page = admin_client.get("/stock/").content.decode()
    nav = page[page.index('<nav class="mainnav"') : page.index("</nav>")]
    assert ">Stock<" in nav and ">Materials<" not in nav
    assert "Wood screw #8" in page and "Danish oil" in page and "Not tracked" in page
    from trammel.core import access
    from trammel.core.navigation import menu_for

    settings_menu = [i.label for i in menu_for(access.as_actor(admin), "admin")]
    assert "Materials" not in settings_menu and "Stock locations" in settings_menu
    low = admin_client.get("/stock/?show=untracked").content.decode()
    assert "Danish oil" in low and "Wood screw #8" not in low
    detail = admin_client.get(f"/shop/materials/{plain.pk}/").content.decode()
    assert 'href="/stock/">Materials</a>' in detail
    # With stock switched off, Materials is back in the menu with its own list.
    stock.set_tracking(screws, tracked=False)
    modules.set_enabled("purchasing", False)
    modules.set_enabled("stock", False)
    page = admin_client.get("/shop/materials/").content.decode()
    nav = page[page.index('<nav class="mainnav"') : page.index("</nav>")]
    assert ">Materials<" in nav and ">Stock<" not in nav and "Danish oil" in page
