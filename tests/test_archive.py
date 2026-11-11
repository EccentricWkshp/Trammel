"""One archive and delete pattern everywhere, and the other Phase 2a loose ends:
a product's cost from its bill of materials, search, the shop-floor prompt for
planned materials and its home section."""

from decimal import Decimal

import pytest

from tests.conftest import make_user
from tests.test_bom_returns import _work_order_for, board, oil, shelf, walnut  # noqa: F401
from trammel.core import archive
from trammel.quotes.forms import RevisionForm
from trammel.quotes.models import QuoteTemplate
from trammel.shop.models import Machine, Material, Vendor
from trammel.stock import services as stock
from trammel.stock.models import Location, StockItem
from trammel.workorders import services as wos

D = Decimal


# ---------------------------------------------------------------------------
# What counts as in use
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_material_kept_in_stock_but_never_stocked_can_be_deleted(admin_client, shelf):  # noqa: F811
    m = Material.objects.create(name="Pine", unit="board_ft", unit_cost=D("3"))
    stock.set_tracking(m, tracked=True, default_location=shelf)
    assert not archive.in_use(m)
    page = admin_client.get(f"/shop/material/{m.pk}/delete/").content.decode()
    assert "Nothing uses this material" in page
    admin_client.post(f"/shop/material/{m.pk}/delete/")
    assert not Material.objects.filter(pk=m.pk).exists()
    assert not StockItem.objects.filter(material_id=m.pk).exists()  # went with it


@pytest.mark.django_db
def test_stocked_or_listed_materials_are_archived_not_deleted(admin_client, board, walnut, oil):  # noqa: F811
    assert archive.in_use(oil)  # on a bill of materials
    stock.receive(walnut, quantity=D("5"), total_cost=D("60"))
    assert archive.in_use(walnut)  # it has lots
    page = admin_client.get(f"/shop/material/{walnut.pk}/delete/").content.decode()
    assert "can't be deleted" in page and "Archive it" in page
    admin_client.post(f"/shop/material/{walnut.pk}/delete/")
    assert Material.objects.filter(pk=walnut.pk).exists()
    admin_client.post(f"/shop/material/{walnut.pk}/archive/", {"archived": "1"})
    walnut.refresh_from_db()
    assert not walnut.is_active
    assert "Archived" in admin_client.get(walnut.get_absolute_url()).content.decode()


@pytest.mark.django_db
def test_vendor_and_machine_archive_and_delete(admin_client, setup_done):
    vendor = Vendor.objects.create(name="Plywood Supply")
    machine = Machine.objects.create(name="Old lathe", kind="other", hourly_rate=D("20"))
    page = admin_client.get(vendor.get_absolute_url()).content.decode()
    assert "Archive" in page and f"/shop/vendor/{vendor.pk}/delete/" in page
    assert 'name="is_active"' not in page
    Material.objects.create(name="Birch ply", unit="sheet", vendor=vendor)
    assert archive.in_use(vendor)  # a material names it
    admin_client.post(f"/shop/vendor/{vendor.pk}/archive/", {"archived": "1"})
    vendor.refresh_from_db()
    assert not vendor.is_active
    admin_client.post(f"/shop/machine/{machine.pk}/delete/")
    assert not Machine.objects.filter(pk=machine.pk).exists()
    assert Machine.all_objects.get(pk=machine.pk).is_deleted


@pytest.mark.django_db
def test_machines_materials_and_vendors_each_have_one_permission(client, setup_done):
    """Adding, editing, archiving and deleting a kind of record go together; the
    kinds stay apart (machines may be the shop's, vendors an admin's)."""
    machine = Machine.objects.create(name="Drill press", kind="other", hourly_rate=D("15"))
    material = Material.objects.create(name="Dowels", unit="each")
    vendor = Vendor.objects.create(name="Dowel Depot")
    client.force_login(make_user("floor@example.com", "shop.view", "shop.manage_machines"))
    page = client.get(machine.get_absolute_url()).content.decode()
    assert "Archive" in page and f"/shop/machine/{machine.pk}/delete/" in page
    assert "Archive" not in client.get(material.get_absolute_url()).content.decode()
    assert client.get(vendor.get_absolute_url()).status_code == 403  # its edit page
    assert (
        client.post(f"/shop/material/{material.pk}/archive/", {"archived": "1"}).status_code == 403
    )
    assert client.post(f"/shop/material/{material.pk}/delete/").status_code == 403
    assert client.post(f"/shop/vendor/{vendor.pk}/delete/").status_code == 403
    client.post(f"/shop/machine/{machine.pk}/archive/", {"archived": "1"})
    machine.refresh_from_db()
    assert not machine.is_active
    client.post(f"/shop/machine/{machine.pk}/delete/")
    assert not Machine.objects.filter(pk=machine.pk).exists()
    assert Material.objects.filter(pk=material.pk, is_active=True).exists()
    assert Vendor.objects.filter(pk=vendor.pk).exists()


@pytest.mark.django_db
def test_editing_shop_records_keeps_what_roles_could_do(setup_done):
    """Upgrading: a role or API key that could edit machines, materials and vendors
    gets all three permissions."""
    from importlib import import_module

    from django.apps import apps as django_apps

    from trammel.accounts.models import Role

    role = Role.objects.create(name="Old shop", permissions=["shop.view", "shop.edit"])
    import_module("trammel.shop.migrations.0005_manage_permissions").forward(django_apps, None)
    role.refresh_from_db()
    assert role.permissions == [
        "shop.manage_machines",
        "shop.manage_materials",
        "shop.manage_vendors",
        "shop.view",
    ]


@pytest.mark.django_db
def test_location_deleted_only_while_nothing_was_kept_there(admin_client, walnut, shelf):  # noqa: F811
    empty = stock.save_location(Location(name="Loft"))
    admin_client.post(f"/stock/locations/{empty.pk}/delete/")
    assert not Location.objects.filter(pk=empty.pk).exists()
    assert archive.in_use(shelf)  # walnut's default location
    rack = stock.save_location(Location(name="Rack"))
    lot = stock.receive(walnut, quantity=D("2"), total_cost=D("24"), location=rack)
    stock.scrap(lot, quantity=D("2"), note="Split")
    assert archive.in_use(rack)  # empty now, but its history stays
    admin_client.post(f"/stock/locations/{rack.pk}/archive/", {"archived": "1"})
    rack.refresh_from_db()
    assert not rack.is_active


@pytest.mark.django_db
def test_default_quote_template_is_kept_and_archived_ones_leave_the_picker(admin_client):
    default = QuoteTemplate.default()
    page = admin_client.get(f"/quotes/templates/{default.pk}/delete/").content.decode()
    assert "Make another template the default first" in page
    admin_client.post(f"/quotes/templates/{default.pk}/archive/", {"archived": "1"})
    default.refresh_from_db()
    assert default.is_active
    spare = QuoteTemplate.objects.create(name="Spare", body="<p>{{ quote.number }}</p>")
    admin_client.post(f"/quotes/templates/{spare.pk}/archive/", {"archived": "1"})
    spare.refresh_from_db()
    assert not spare.is_active
    assert spare not in RevisionForm().fields["template"].queryset
    admin_client.post(f"/quotes/templates/{spare.pk}/delete/")
    assert not QuoteTemplate.objects.filter(pk=spare.pk).exists()


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_read_only_product_page(client, board):  # noqa: F811
    client.force_login(make_user("viewer@example.com", "catalog.view"))
    assert board.get_absolute_url() in client.get("/products/").content.decode()
    page = client.get(board.get_absolute_url()).content.decode()
    assert "Cutting board" in page and "<form" not in page.split('class="tab-body"')[1]
    assert "Archive" not in page
    assert client.post(board.get_absolute_url(), {"name": "Changed"}).status_code == 403
    bom = client.get(f"{board.get_absolute_url()}?tab=bom").content.decode()
    assert "Walnut" in bom and "Add a material" not in bom


@pytest.mark.django_db
def test_product_cost_is_calculated_from_its_bill_of_materials(admin_client, board):  # noqa: F811
    from trammel.catalog import costing
    from trammel.catalog import services as catalog

    # 2.5 board feet of walnut at $12 and 50 ml of oil at $40 a liter: $32.
    costing.refresh([board])
    board.refresh_from_db()
    assert board.cost == D("32")
    board.other_cost = D("20")
    catalog.save_product(board)
    assert board.cost == D("52")
    # Walnut goes up: the cost follows, the $20 of other cost stays.
    wood = Material.objects.get(name="Walnut")
    Material.objects.filter(pk=wood.pk).update(unit_cost=D("14"))
    stock.refresh_costs_using(wood)
    board.refresh_from_db()
    assert board.cost == D("57")
    page = admin_client.get(f"{board.get_absolute_url()}?tab=cost").content.decode()
    assert "$37.00" in page and "$57.00" in page and "calculated cost" in page


# ---------------------------------------------------------------------------
# Search, the shop floor and the home screen
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_search_finds_locations_vendors_and_purchase_orders(admin_client, shelf, admin):  # noqa: F811
    from trammel.purchasing import services as purchasing

    vendor = Vendor.objects.create(name="Hardwood Haven", account_number="HH-881")
    po = purchasing.create_order(vendor, notes="Rush the walnut")
    page = admin_client.get("/search/?q=shelf").content.decode()
    assert "Stock locations" in page and shelf.get_absolute_url() in page
    page = admin_client.get("/search/?q=HH-881").content.decode()
    assert "Vendors" in page and vendor.get_absolute_url() in page
    page = admin_client.get("/search/?q=haven").content.decode()
    assert "Purchase orders" in page and po.get_absolute_url() in page


@pytest.mark.django_db
def test_floor_offers_to_record_planned_materials_and_home_lists_them(
    admin_client,
    board,  # noqa: F811
    walnut,  # noqa: F811
    admin,
):
    stock.receive(walnut, quantity=D("20"), total_cost=D("240.00"))
    wo = _work_order_for(board, 1, admin)
    waiting = f"{wo.get_absolute_url()}?tab=materials"  # the home section's link
    home = admin_client.get("/").content.decode()
    assert "Materials waiting to be recorded" in home and waiting not in home
    wos.set_operation(wo.operations.get(), "done", user=admin)
    assert waiting in admin_client.get("/").content.decode()
    floor = admin_client.get(f"/floor/wo/{wo.pk}/").content.decode()
    assert "Record the planned materials?" in floor
    response = admin_client.post(
        f"/work-orders/{wo.pk}/materials/planned/", {"next": f"/floor/wo/{wo.pk}/"}
    )
    assert response["Location"] == f"/floor/wo/{wo.pk}/"  # back to the floor
    assert list(wos.ready_to_record(wo)) == []
    assert waiting not in admin_client.get("/").content.decode()
