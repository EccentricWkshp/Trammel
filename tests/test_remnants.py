"""Remnants: offcuts kept with their size and their share of the cost (FR-STK-5)."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.shop.models import Material, MaterialUnit
from trammel.stock import services as stock
from trammel.stock.models import Location, StockLot
from trammel.workorders import services as wos
from trammel.workorders.models import MaterialUsage

D = Decimal


@pytest.fixture
def rack(setup_done):
    return stock.save_location(Location(name="Sheet rack"))


@pytest.fixture
def offcuts(setup_done):
    return stock.save_location(Location(name="Offcut bin"))


@pytest.fixture
def ply(rack):
    """Plywood counted in 4 × 8 ft sheets: a sheet is 4608 in²."""
    m = Material.objects.create(
        name="Birch ply 3/4",
        unit="sheet",
        unit_cost=D("40"),
        sheet_width=D("4"),
        sheet_length=D("8"),
        sheet_dimension_unit="ft",
    )
    stock.set_tracking(m, tracked=True, default_location=rack)
    return m


@pytest.fixture
def oak(rack):
    """Oak by the board foot, 6 in wide: a linear foot is half a board foot."""
    m = Material.objects.create(name="Oak 4/4 x 6", unit="board_ft", unit_cost=D("9"))
    MaterialUnit.objects.create(
        material=m, quantity=D("1"), unit="linear_ft", stock_quantity=D("0.5")
    )
    stock.set_tracking(m, tracked=True, default_location=rack)
    return m


@pytest.fixture
def wo(admin):
    job = jobs.create_job(Job(title="Cabinet", customer=Customer.internal()))
    jobs.transition(job, "approved", actor=admin)
    return wos.create(job, title="Carcass")


def _use(wo, material, qty, unit="", **kw):
    return wos.record_material(wo, material=material, quantity=D(qty), unit=unit, **kw)


@pytest.mark.django_db
def test_leftover_piece_goes_back_and_the_job_pays_for_the_rest(ply, wo, admin):
    sheets = stock.receive(ply, quantity=D("2"), total_cost=D("80.00"))
    [usage] = _use(wo, ply, "1", leftover=(D("24"), D("18"), "in"), user=admin)
    # 24 × 18 in is 432 / 4608 = 0.09375 of a sheet, kept at 0.0938.
    piece = StockLot.objects.get(origin="remnant")
    assert piece.size == "24 × 18 in" and piece.parent == sheets
    assert (piece.qty_on_hand, piece.value_on_hand) == (D("0.0938"), D("3.75"))
    assert piece.location == sheets.location and piece.received_on == sheets.received_on
    assert usage.total_cost == D("36.25") and usage.quantity == D("0.9062")
    assert stock.check_ledger() == []
    # What went back as a remnant cannot be returned again.
    with pytest.raises(ValidationError, match="no more than that"):
        _use(wo, ply, "-1")
    [back] = _use(wo, ply, "-0.9062")
    assert back.total_cost == D("-36.25")
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_remnants_are_used_only_when_picked_and_whole(ply, wo, admin):
    stock.receive(ply, quantity=D("1"), total_cost=D("40.00"))
    _use(wo, ply, "0.5", leftover=(D("48"), D("24"), "in"), user=admin)  # keep 2 × 4 ft
    piece = StockLot.objects.get(origin="remnant")
    # Oldest-first draws ignore the remnant; asking for more than the sheets explains why.
    with pytest.raises(ValidationError, match="also 1 remnant, used only when picked"):
        _use(wo, ply, "1")
    with pytest.raises(ValidationError, match="used whole"):
        _use(wo, ply, "0.1", lot=piece.number)
    # Use it whole, keeping a smaller offcut of it.
    [usage] = _use(wo, ply, str(piece.qty_on_hand), lot=piece.pk, leftover=(D("12"), D("12"), "in"))
    piece.refresh_from_db()
    smaller = StockLot.objects.get(parent=piece)
    assert piece.qty_on_hand == 0 and smaller.size == "12 × 12 in"
    assert usage.total_cost + smaller.value_on_hand == D("10.00")
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_cut_a_remnant_off_a_lot(ply, offcuts, admin):
    sheet = stock.receive(ply, quantity=D("1"), total_cost=D("40.00"))
    piece = stock.cut_remnant(
        sheet, length=D("48"), width=D("48"), unit="in", location=offcuts, user=admin
    )
    sheet.refresh_from_db()
    assert (piece.qty_on_hand, piece.value_on_hand, piece.location) == (
        D("0.5"),
        D("20.00"),
        offcuts,
    )
    assert (sheet.qty_on_hand, sheet.value_on_hand) == (D("0.5"), D("20.00"))
    with pytest.raises(ValidationError, match="all of it"):
        stock.transfer(piece, quantity=D("0.25"), to=sheet.location)
    with pytest.raises(ValidationError, match="all of it"):
        stock.scrap(piece, quantity=D("0.25"), note="Chipped")
    moved = stock.transfer(piece, quantity=D("0.5"), to=sheet.location)
    assert moved.pk == piece.pk and moved.size == "48 × 48 in"
    with pytest.raises(ValidationError, match="has only"):
        stock.cut_remnant(sheet, length=D("96"), width=D("48"), unit="in")
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_boards_and_bars_are_measured_by_length(oak, wo):
    board = stock.receive(oak, quantity=D("10"), total_cost=D("90.00"))
    piece = stock.cut_remnant(board, length=D("30"), unit="in")  # 2.5 ft = 1.25 board ft
    assert piece.qty_on_hand == D("1.25") and piece.size == "30 in"
    with pytest.raises(ValidationError, match="cannot be measured by area"):
        stock.cut_remnant(board, length=D("30"), width=D("6"), unit="in")


@pytest.mark.django_db
def test_find_remnants_either_way_round_smallest_first(ply, oak, admin):
    sheet = stock.receive(ply, quantity=D("2"), total_cost=D("80.00"))
    big = stock.cut_remnant(sheet, length=D("36"), width=D("30"), unit="in")
    small = stock.cut_remnant(sheet, length=D("18"), width=D("24"), unit="in")
    board = stock.receive(oak, quantity=D("10"), total_cost=D("90.00"))
    stock.cut_remnant(board, length=D("40"), unit="in")
    fits = list(stock.remnants(ply, length=D("20"), width=D("16"), unit="in"))
    assert fits == [small, big]  # 18 × 24 fits 20 × 16 turned round
    assert list(stock.remnants(ply, length=D("25"), width=D("25"), unit="in")) == [big]
    assert list(stock.remnants(length=D("2"), width=D("2"), unit="ft")) == [big]
    assert len(stock.remnants(length=D("1"), unit="m")) == 1  # only the 40 in board


@pytest.mark.django_db
def test_counts_leave_remnants_alone(ply, rack):
    sheets = stock.receive(ply, quantity=D("3"), total_cost=D("120.00"))
    stock.cut_remnant(sheets, length=D("48"), width=D("48"), unit="in")
    assert stock.on_hand(stock.item_for(ply), rack, remnants=False) == D("2.5")
    stock.count(ply, location=rack, counted=D("2"), expected=D("2.5"))
    assert StockLot.objects.get(origin="remnant").qty_on_hand == D("0.5")
    assert stock.check_ledger() == []


@pytest.mark.django_db
def test_remnant_pages_labels_and_forms(admin_client, ply, wo, offcuts):
    sheets = stock.receive(ply, quantity=D("2"), total_cost=D("80.00"))
    response = admin_client.post(
        f"/stock/lots/{sheets.pk}/remnant/",
        {"length": "30", "width": "20", "unit": "in", "location": str(offcuts.pk)},
    )
    assert response.status_code == 302
    piece = StockLot.objects.get(origin="remnant")
    found = admin_client.get("/stock/remnants/?length=18&width=28&unit=in").content.decode()
    assert piece.number in found and "30 × 20 in" in found
    assert (
        piece.number not in admin_client.get("/stock/remnants/?length=31&width=21").content.decode()
    )
    tab = admin_client.get(f"/shop/materials/{ply.pk}/?tab=stock").content.decode()
    assert tab.index("remnant 30 × 20 in") < tab.index(sheets.number)
    floor = admin_client.get(f"/stock/f/lot/{piece.pk}/").content.decode()
    assert "Use this remnant" in floor and "The whole remnant" in floor
    label = admin_client.get(f"/stock/labels/print/?lot={piece.pk}").content.decode()
    assert "Remnant 30 × 20 in" in label
    page = admin_client.get(f"{wo.get_absolute_url()}?tab=materials").content.decode()
    assert 'name="leftover_length"' in page
    admin_client.post(
        f"/work-orders/{wo.pk}/materials/",
        {
            "material": str(ply.pk),
            "quantity": "1",
            "leftover_length": "40",
            "leftover_width": "20",
            "leftover_unit": "in",
        },
    )
    assert StockLot.objects.filter(origin="remnant").count() == 2
    assert MaterialUsage.objects.get(work_order=wo).total_cost < D("40.00")


@pytest.mark.django_db
def test_untracked_material_keeps_no_leftover(setup_done, wo):
    glue = Material.objects.create(name="Glue", unit="l", unit_cost=D("9"))
    with pytest.raises(ValidationError, match="not kept in stock"):
        _use(wo, glue, "1", leftover=(D("1"), None, "in"))
