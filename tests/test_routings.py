"""Product routings (Phase 2b): the steps it takes to make a product, their time
and cost, and the operations a work order gets from them."""

import datetime as dt
import json
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from tests.conftest import make_user
from trammel.accounts.models import ApiKey
from trammel.catalog import costing
from trammel.catalog import services as catalog
from trammel.catalog.models import Product, RoutingStep
from trammel.core import access
from trammel.core.models import ShopRates
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.shop.models import Machine, Material
from trammel.stock import services as stock
from trammel.stock.models import BOMLine
from trammel.timetracking import services as time
from trammel.workorders import services as wos

D = Decimal


@pytest.fixture
def rates(setup_done):
    r = ShopRates.load()
    r.labor_hourly_rate, r.shop_overhead_rate = D("60"), D("0")
    r.save()
    return r


@pytest.fixture
def laser(setup_done):
    return Machine.objects.create(name="Laser", kind="laser", hourly_rate=D("90"))


@pytest.fixture
def walnut(setup_done):
    return Material.objects.create(name="Walnut", unit="board_ft", unit_cost=D("12"))


@pytest.fixture
def board(rates, laser, walnut):
    """A cutting board: 2.5 board feet of walnut, cut and engraved on the laser
    (30 minutes to set up a batch of 10, then 12 minutes each), and 5 minutes of
    sanding each."""
    product = catalog.save_product(
        Product(name="Cutting board", kind="made", price=D("85"), batch_size=10)
    )
    engrave = catalog.save_step(
        RoutingStep(
            product=product,
            name="Cut and engrave",
            machine=laser,
            setup_minutes=D("30"),
            machine_minutes=D("12"),
            instructions="Grain runs the long way.",
        )
    )
    sand = catalog.save_step(RoutingStep(product=product, name="Sand", labor_minutes=D("5")))
    stock.save_bom_line(
        BOMLine(product=product, material=walnut, quantity=D("2.5"), unit="board_ft", step=engrave)
    )
    product.refresh_from_db()
    return product, engrave, sand


@pytest.mark.django_db
def test_cost_is_materials_labor_setup_and_machine(board):
    product, _engrave, _sand = board
    parts = {p.label: p.amount for p in costing.parts(product)}
    # Walnut 2.5 × 12 = 30; sanding 5 min at 60/h = 5; setup 30 min over 10 at
    # 60/h = 3; laser 12 min at 90/h = 18.
    assert parts == {
        "Materials": D("30"),
        "Labor": D("5"),
        "Setup": D("3"),
        "Machine time": D("18"),
    }
    assert product.cost == D("56") == costing.calculated(product)


@pytest.mark.django_db
def test_an_override_wins_and_clearing_it_brings_the_calculation_back(admin_client, board):
    product, *_ = board
    product.cost_override = D("40")
    catalog.save_product(product)
    assert product.cost == D("40")
    page = admin_client.get(f"{product.get_absolute_url()}?tab=cost").content.decode()
    assert "set by hand" in page and "$56.00" in page
    product.cost_override = None
    catalog.save_product(product)
    assert product.cost == D("56")


@pytest.mark.django_db
def test_the_calculation_follows_machine_and_labor_rates(board, laser, rates):
    product, *_ = board
    laser.hourly_rate = D("120")  # 12 min: 18 → 24
    laser.save()
    product.refresh_from_db()
    assert product.cost == D("62")
    rates.labor_hourly_rate = D("120")  # labor 5 → 10, setup 3 → 6
    rates.save()
    product.refresh_from_db()
    assert product.cost == D("70")
    assert costing.refresh_all() == 0  # already up to date


@pytest.mark.django_db
def test_labor_is_costed_at_the_labor_cost_not_the_rate_charged(board, rates, admin):
    product, *_ = board
    rates.labor_cost_rate = D("30")  # charged at 60, costs 30: labor 5 → 2.50, setup 3 → 1.50
    rates.save()
    product.refresh_from_db()
    assert product.cost == D("30") + D("2.5") + D("1.5") + D("18")
    labor = next(p for p in costing.parts(product) if p.label == "Labor")
    assert "$30.00 an hour" in labor.detail

    # Logged time by someone without a cost rate of their own is costed the same way.
    job = jobs.create_job(Job(title="Shop time", customer=Customer.internal()))
    jobs.transition(job, "approved", actor=admin)
    wo = wos.create(job)
    admin.labor_cost_rate = None
    admin.save()
    entry = time.manual(wo, kind="labor", minutes=D("60"), started_at=timezone.now(), user=admin)
    assert (entry.cost_rate, entry.bill_rate, entry.cost) == (D("30"), D("60"), D("30.00"))


@pytest.mark.django_db
def test_steps_are_added_edited_moved_and_removed_on_the_product_page(admin_client, board, laser):
    product, engrave, sand = board
    url = product.get_absolute_url()
    page = admin_client.get(f"{url}?tab=routing").content.decode()
    assert "Cut and engrave" in page and "Add a step" in page
    admin_client.post(
        f"/products/{product.pk}/routing/",
        {"name": "Oil", "setup_minutes": "0", "machine_minutes": "0", "labor_minutes": "2"},
    )
    assert [s.name for s in product.routing_steps.all()] == ["Cut and engrave", "Sand", "Oil"]
    admin_client.post(f"/products/{product.pk}/routing/{sand.pk}/move/", {"direction": "up"})
    assert [s.name for s in product.routing_steps.all()][0] == "Sand"
    admin_client.post(
        f"/products/{product.pk}/routing/{sand.pk}/",
        {"name": "Sand to 220", "setup_minutes": "0", "machine_minutes": "0", "labor_minutes": "6"},
    )
    sand.refresh_from_db()
    assert (sand.name, sand.labor_minutes) == ("Sand to 220", D("6"))
    # Machine time needs its machine.
    admin_client.post(
        f"/products/{product.pk}/routing/",
        {"name": "Plane", "setup_minutes": "0", "machine_minutes": "3", "labor_minutes": "0"},
    )
    assert not product.routing_steps.filter(name="Plane").exists()
    admin_client.post(f"/products/{product.pk}/routing/{sand.pk}/delete/")
    assert not product.routing_steps.filter(pk=sand.pk).exists()
    product.refresh_from_db()
    assert product.cost == D("30") + D("3") + D("18") + D("2")  # oil's 2 min, no sanding

    viewer = make_user("v@example.com", "catalog.view")
    client = Client()
    client.force_login(viewer)
    assert client.post(f"/products/{product.pk}/routing/", {"name": "x"}).status_code == 403
    assert "Add a step" not in client.get(f"{url}?tab=routing").content.decode()


def _quote(job, product, quantity):
    revision = quotes.create_quote(job)
    form = LINE_FORMS["product"]({"product": str(product.pk), "quantity": str(quantity)})
    assert form.is_valid(), form.errors
    quotes.save_line(form.build(revision))
    return revision


@pytest.fixture
def work_order(board, customer, admin):
    product, *_ = board
    job = jobs.create_job(Job(title="Boards", customer=customer))
    revision = _quote(job, product, 3)
    quotes.send(revision, actor=admin)
    quotes.approve(revision, actor=admin, approved_by_name="Grace", accept_agreement=True)
    job.refresh_from_db()
    return wos.create(job, revision=revision)


@pytest.mark.django_db
def test_a_work_order_gets_the_steps_with_their_time_and_materials(board, work_order):
    _product, engrave, _sand = board
    [line_op] = work_order.operations.filter(parent=None)
    steps = list(line_op.children.order_by("position"))
    assert [s.name for s in steps] == ["Cut and engrave", "Sand"]
    cut, sand = steps
    assert (cut.machine.name, cut.description) == ("Laser", "Grain runs the long way.")
    assert (cut.planned_labor_minutes, cut.planned_machine_minutes) == (D("30"), D("36"))
    assert (sand.planned_labor_minutes, sand.planned_machine_minutes) == (D("15"), D("0"))
    planned = work_order.planned_materials.get()
    assert planned.operation == cut and planned.quantity == D("7.5")
    tree = {op.pk: op for op in wos.operation_tree(work_order)}
    assert tree[line_op.pk].time["planned_labor"] == D("45")
    assert tree[line_op.pk].time["planned_machine"] == D("36")


@pytest.mark.django_db
def test_change_orders_bring_their_products_routings(board, work_order, admin):
    product, *_ = board
    change = _quote(work_order.job, product, 2)
    assert change.quote.is_change_order
    quotes.send(change, actor=admin)
    quotes.approve(change, actor=admin, approved_by_name="Grace")
    co_op = work_order.operations.get(source_revision=change)
    [line_op] = co_op.children.all()
    assert [s.name for s in line_op.children.order_by("position")] == ["Cut and engrave", "Sand"]
    assert line_op.children.get(name="Sand").planned_labor_minutes == D("10")


@pytest.mark.django_db
def test_logged_time_shows_against_the_plan(admin_client, admin, work_order):
    sand = work_order.operations.get(name="Sand")
    time.manual(
        work_order,
        kind="labor",
        minutes=D("20"),
        started_at=timezone.now() - dt.timedelta(hours=1),
        user=admin,
        operation=sand,
    )
    page = admin_client.get(f"/work-orders/{work_order.pk}/?tab=operations").content.decode()
    assert "Planned 15 min hands-on" in page and "Logged 20 min hands-on" in page
    assert 'class="over"' in page
    floor = admin_client.get(f"/floor/wo/{work_order.pk}/").content.decode()
    assert "Logged 20 min hands-on" in floor
    traveler = admin_client.get(f"/work-orders/{work_order.pk}/traveler/").content.decode()
    assert "Planned 15 min hands-on" in traveler


@pytest.mark.django_db
def test_planned_time_can_be_typed_on_any_operation(admin_client, work_order):
    op = wos.add_operation(work_order, name="Pack")
    admin_client.post(
        f"/work-orders/{work_order.pk}/operations/{op.pk}/edit/",
        {"name": "Pack", "planned_labor_minutes": "10", "planned_machine_minutes": ""},
    )
    op.refresh_from_db()
    assert (op.planned_labor_minutes, op.planned_machine_minutes) == (D("10"), None)


@pytest.mark.django_db
def test_the_api_keeps_routings_and_shows_cost_and_time(admin, board, laser, work_order):
    product, engrave, _sand = board
    scopes = sorted(access.permissions_of(access.as_actor(admin)))
    issued = ApiKey.issue(admin, "t", scopes)
    token = issued[1] if isinstance(issued, tuple) else issued
    client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")

    def call(method, path, body=None):
        return getattr(client, method)(
            "/api/v1" + path,
            data=json.dumps(body) if body is not None else None,
            content_type="application/json",
        )

    response = call(
        "post",
        f"/catalog/{product.pk}/routing",
        {"name": "Oil", "labor_minutes": "2", "wait_hours": "24"},
    )
    assert response.status_code == 201, response.content
    assert D(response.json()["wait_hours"]) == D("24")
    steps = call("get", f"/catalog/{product.pk}/routing").json()
    assert [s["name"] for s in steps] == ["Cut and engrave", "Sand", "Oil"]
    body = call("get", "/catalog/").json()
    found = next(p for p in body["items"] if p["id"] == str(product.pk))
    assert found["cost_override"] is None and found["batch_size"] == 10
    assert {p["label"] for p in found["cost_breakdown"]} >= {"Materials", "Setup", "Machine time"}
    created = call(
        "post", "/catalog/", {"name": "Resold oil", "kind": "resold", "cost": "5"}
    ).json()
    assert D(created["cost_override"]) == D("5") == D(created["cost"])
    body = call("get", f"/work-orders/{work_order.pk}").json()
    assert body["estimated_finish"] is not None
    ops = body["operations"]
    cut = next(o for o in ops if o["name"] == "Cut and engrave")
    assert D(cut["planned_machine_minutes"]) == D("36") and D(cut["logged_labor_minutes"]) == 0
