"""Bills of materials, recording planned materials, vendor returns and landed cost
changes (FR-STK-6, FR-PUR-5)."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from trammel.catalog.models import Product
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.purchasing import services as purchasing
from trammel.purchasing.models import CostVariance, POLine
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.shop.models import Material, Vendor
from trammel.stock import services as stock
from trammel.stock.models import BOMLine, Location, StockLot, StockSettings
from trammel.workorders import services as wos
from trammel.workorders.models import MaterialUsage

D = Decimal


@pytest.fixture
def shelf(setup_done):
    return stock.save_location(Location(name="Shelf"))


@pytest.fixture
def walnut(shelf):
    m = Material.objects.create(name="Walnut", unit="board_ft", unit_cost=D("12"))
    stock.set_tracking(m, tracked=True, default_location=shelf)
    return m


@pytest.fixture
def oil(setup_done):
    return Material.objects.create(name="Danish oil", unit="l", unit_cost=D("40"))


@pytest.fixture
def board(walnut, oil):
    """A cutting board: 2.5 board feet of walnut and 50 ml of oil each."""
    product = Product.objects.create(name="Cutting board", kind="made", price=D("85"))
    stock.save_bom_line(
        BOMLine(product=product, material=walnut, quantity=D("2.5"), unit="board_ft")
    )
    stock.save_bom_line(BOMLine(product=product, material=oil, quantity=D("50"), unit="ml"))
    return product


def _work_order_for(product, quantity, admin):
    job = jobs.create_job(Job(title="Boards", customer=Customer.internal()))
    revision = quotes.create_quote(job)
    form = LINE_FORMS["product"]({"product": str(product.pk), "quantity": str(quantity)})
    assert form.is_valid(), form.errors
    quotes.save_line(form.build(revision))
    jobs.transition(job, "approved", actor=admin)
    return wos.create(job, revision=revision)


@pytest.mark.django_db
def test_a_product_s_bill_of_materials_is_planned_on_the_work_order(board, walnut, admin):
    wo = _work_order_for(board, 3, admin)
    planned = {p.material.name: p for p in wo.planned_materials.select_related("material")}
    assert (planned["Walnut"].quantity, planned["Walnut"].unit) == (D("7.5"), "board_ft")
    assert (planned["Danish oil"].quantity, planned["Danish oil"].unit) == (D("150"), "ml")
    [op] = wo.operations.all()
    assert planned["Walnut"].operation == op


@pytest.mark.django_db
def test_done_operation_asks_then_records_its_planned_materials(admin_client, board, walnut, admin):
    stock.receive(walnut, quantity=D("20"), total_cost=D("240.00"))
    wo = _work_order_for(board, 2, admin)
    assert list(wos.ready_to_record(wo)) == []  # not done yet
    op = wo.operations.get()
    wos.set_operation(op, "done", user=admin)
    page = admin_client.get(f"{wo.get_absolute_url()}?tab=materials").content.decode()
    assert "Record the planned materials?" in page
    oil_line = wo.planned_materials.get(material__name="Danish oil")
    admin_client.post(f"/work-orders/{wo.pk}/materials/planned/", {"planned": [str(oil_line.pk)]})
    assert MaterialUsage.objects.filter(work_order=wo).count() == 1  # only what was ticked
    admin_client.post(f"/work-orders/{wo.pk}/materials/planned/")
    used = {u.description: u for u in MaterialUsage.objects.filter(work_order=wo)}
    assert used["Walnut"].quantity == D("5") and used["Walnut"].source == "stock"
    assert used["Danish oil"].total_cost == D("4.00")  # 100 ml at $40 a liter
    assert list(wos.ready_to_record(wo)) == []
    assert stock.on_hand(stock.item_for(walnut)) == D("15")


@pytest.mark.django_db
def test_automatic_mode_records_when_done_but_never_blocks(board, walnut, admin):
    settings = StockSettings.load()
    settings.planned_use = "auto"
    settings.save()
    stock.receive(walnut, quantity=D("2"), total_cost=D("24.00"))  # not enough for 2 boards
    wo = _work_order_for(board, 2, admin)
    op = wo.operations.get()
    wos.set_operation(op, "done", user=admin)
    op.refresh_from_db()
    assert op.status == "done"  # stock was short, but the operation is done
    assert MaterialUsage.objects.filter(work_order=wo).count() == 0
    assert len(wos.ready_to_record(wo)) == 2  # waiting on the prompt
    stock.receive(walnut, quantity=D("10"), total_cost=D("120.00"))
    wo2 = _work_order_for(board, 1, admin)
    wos.set_operation(wo2.operations.get(), "done", user=admin)
    used = MaterialUsage.objects.filter(work_order=wo2)
    # One usage per lot drawn from: the walnut came from two lots.
    assert {u.description for u in used} == {"Walnut", "Danish oil"}
    assert sum(u.quantity for u in used if u.description == "Walnut") == D("2.5")


@pytest.mark.django_db
def test_bom_on_the_product_page(admin_client, board, walnut):
    details = admin_client.get(board.get_absolute_url()).content.decode()
    assert "?tab=bom" in details and "Walnut" not in details  # its own tab
    page = admin_client.get(f"{board.get_absolute_url()}?tab=bom").content.decode()
    assert "Bill of materials" in page and "Walnut" in page and "$32.00" in page  # 30 + 2
    response = admin_client.post(
        f"/stock/products/{board.pk}/bom/",
        {"material": str(walnut.pk), "quantity": "0.5", "unit": "board_ft", "note": "handle"},
    )
    assert response["Location"].endswith("?tab=bom")
    assert board.bom_lines.count() == 3
    line = board.bom_lines.get(note="handle")
    admin_client.post(f"/stock/bom/{line.pk}/delete/")
    assert board.bom_lines.count() == 2


@pytest.mark.django_db
def test_bom_line_edited_in_place(admin_client, board, walnut, oil):
    line = board.bom_lines.get(material=walnut)
    page = admin_client.get(f"{board.get_absolute_url()}?tab=bom&edit={line.pk}").content.decode()
    assert 'id="bom-edit"' in page and 'value="2.5"' in page
    assert "Add a material" not in page  # one form at a time
    url = f"/stock/bom/{line.pk}/"
    admin_client.post(
        url, {"material": str(walnut.pk), "quantity": "3", "unit": "board_ft", "note": "thicker"}
    )
    line.refresh_from_db()
    assert (line.quantity, line.note, line.position) == (D("3"), "thicker", 10)
    assert board.bom_lines.count() == 2
    bad = admin_client.post(url, {"material": str(walnut.pk), "quantity": "2", "unit": "kg"})
    assert bad["Location"].endswith(f"&edit={line.pk}")  # stays open to fix it
    line.refresh_from_db()
    assert line.unit == "board_ft"


@pytest.fixture
def vendor(setup_done):
    return Vendor.objects.create(name="Hardwood Co")


def _received(vendor, material, qty, price, admin, **charges):
    po = purchasing.create_order(vendor, **charges)
    purchasing.save_line(
        POLine(
            order=po, material=material, quantity=D(qty), unit=material.unit, unit_price=D(price)
        )
    )
    purchasing.place(po, actor=admin)
    line = po.lines.get()
    receipt = purchasing.receive(line, quantity=D(qty), user=admin)
    return po, line, receipt


@pytest.mark.django_db
def test_send_part_of_a_delivery_back(vendor, walnut, admin):
    po, line, receipt = _received(vendor, walnut, "10", "12.00", admin)
    with pytest.raises(ValidationError, match="why"):
        purchasing.return_to_vendor(line, quantity=D("2"), refund=D("20"), reason="")
    back = purchasing.return_to_vendor(
        line, quantity=D("2"), refund=D("20.00"), reason="Checked and split", user=admin
    )
    assert (back.value, back.refund, back.loss) == (D("24.00"), D("20.00"), D("4.00"))
    receipt.lot.refresh_from_db()
    assert receipt.lot.qty_on_hand == D("8")
    assert CostVariance.objects.get().amount == D("4.00")
    with pytest.raises(ValidationError, match="not gone back"):
        purchasing.return_to_vendor(line, quantity=D("9"), refund=D("0"), reason="More")
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_late_freight_revalues_what_is_left_and_the_rest_is_a_variance(
    vendor, walnut, admin, shelf
):
    job = jobs.create_job(Job(title="Shelf", customer=Customer.internal()))
    jobs.transition(job, "approved", actor=admin)
    wo = wos.create(job, title="Shelf")
    po, line, receipt = _received(vendor, walnut, "10", "12.00", admin)
    wos.record_material(wo, material=walnut, quantity=D("4"), unit="")  # 40% used
    purchasing.adjust_landed_cost(po, amount=D("20.00"), reason="Freight bill", user=admin)
    lot = StockLot.objects.get(pk=receipt.lot_id)
    assert lot.value_on_hand == D("72.00") + D("12.00")  # 60% of the $20 stays in stock
    assert CostVariance.objects.get(kind="landed").amount == D("8.00")
    assert stock.check_ledger() == []
    with pytest.raises(ValidationError, match="untouched"):
        purchasing.unreceive(receipt)
    purchasing.adjust_landed_cost(po, amount=D("-5.00"), reason="Invoice corrected", user=admin)
    lot.refresh_from_db()
    assert lot.value_on_hand == D("81.00")


@pytest.mark.django_db
def test_landed_cost_waits_until_everything_arrived(admin_client, vendor, walnut, admin):
    po = purchasing.create_order(vendor)
    purchasing.save_line(
        POLine(order=po, material=walnut, quantity=D("10"), unit="board_ft", unit_price=D("12"))
    )
    purchasing.place(po, actor=admin)
    purchasing.receive(po.lines.get(), quantity=D("4"), user=admin)
    with pytest.raises(ValidationError, match="once everything has arrived"):
        purchasing.adjust_landed_cost(po, amount=D("5"), reason="Freight")
    page = admin_client.get(po.get_absolute_url()).content.decode()
    assert "Send something back" in page and "Adjust the landed cost" not in page
    purchasing.receive(po.lines.get(), quantity=D("6"), user=admin)
    admin_client.post(f"/purchasing/{po.pk}/adjust/", {"amount": "12.50", "reason": "Freight"})
    admin_client.post(
        f"/purchasing/{po.pk}/lines/{po.lines.get().pk}/return/",
        {"quantity": "1", "refund": "10.00", "reason": "Cracked"},
    )
    page = admin_client.get(po.get_absolute_url()).content.decode()
    assert "Landed cost changes" in page and "Cracked" in page
    report = admin_client.get("/purchasing/reports/variances/").content.decode()
    assert "Vendor return" in report and po.number in report
