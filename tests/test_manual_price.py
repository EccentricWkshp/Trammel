"""A price set by hand on a quote line, with a required internal reason."""

from decimal import Decimal

import pytest

from tests.test_quotes import job, laser, ply, rates, revision  # noqa: F401 — fixtures
from trammel.catalog.models import Product
from trammel.quotes import services
from trammel.quotes.estimators import ESTIMATORS
from trammel.quotes.forms import LINE_FORMS

D = Decimal


@pytest.fixture
def board(setup_done):
    return Product.objects.create(name="Cutting board", price=D("85.00"), cost=D("22.00"))


def _form(kind, **data):
    return LINE_FORMS[kind](data)


@pytest.mark.django_db
def test_manual_price_replaces_the_catalog_price_without_touching_the_product(
    revision,  # noqa: F811
    board,
):
    form = _form(
        "product",
        product=str(board.pk),
        quantity="3",
        manual_price="75",
        price_note="Repeat customer",
    )
    assert form.is_valid(), form.errors
    line = services.save_line(form.build(revision))
    assert line.unit_price == D("75") and line.line_total == D("225.00")
    assert line.calculated_unit_price == D("85.00") and line.price_note == "Repeat customer"
    assert line.cost_total == D("66.00")  # cost still from the product
    board.refresh_from_db()
    assert board.price == D("85.00")
    revision.refresh_from_db()
    assert revision.subtotal == D("225.00")


@pytest.mark.django_db
def test_manual_price_needs_a_reason(revision, board):  # noqa: F811
    form = _form("product", product=str(board.pk), quantity="1", manual_price="75")
    assert not form.is_valid() and "price_note" in form.errors


@pytest.mark.django_db
def test_editing_keeps_or_clears_the_manual_price(revision, board):  # noqa: F811
    form = _form(
        "product", product=str(board.pk), quantity="1", manual_price="70", price_note="Price match"
    )
    assert form.is_valid()
    line = services.save_line(form.build(revision))
    initial = LINE_FORMS["product"].initial_from(line)
    assert initial["manual_price"] == D("70") and initial["price_note"] == "Price match"

    form = _form("product", product=str(board.pk), quantity="1")  # manual price removed
    assert form.is_valid()
    line = services.save_line(form.build(revision, line))
    assert line.unit_price == D("85.00") and not line.price_is_manual and line.price_note == ""


@pytest.mark.django_db
def test_labor_and_discount_still_apply_on_top(revision):  # noqa: F811
    form = _form(
        "labor",
        description="Assembly",
        quantity="2",
        manual_price="50",
        price_note="Simple job",
        discount_pct="10",
    )
    assert form.is_valid(), form.errors
    line = services.save_line(form.build(revision))
    assert line.calculated_unit_price == D("60") and line.line_total == D("90.00")


@pytest.mark.django_db
def test_an_item_has_no_separate_manual_price(revision):  # noqa: F811
    form = _form("adhoc", description="Widget", quantity="1", unit="each", unit_price="10")
    assert "manual_price" not in form.fields and form.is_valid()
    assert not services.save_line(form.build(revision)).price_is_manual


@pytest.mark.django_db
def test_estimate_with_manual_price_scales_the_costing_split(admin_client, revision, laser, ply):  # noqa: F811
    data = {
        "description": "Coasters",
        "quantity": "10",
        "material": str(ply.pk),
        "area": "16",
        "area_unit": "sq_in",
        "waste_pct": "0",
        "cut_minutes": "3",
        "engrave_minutes": "3",
        "machine": str(laser.pk),
        "labor_minutes": "0",
        "setup_minutes": "30",
    }
    # Without a manual price this estimate is 160.00 (see test_quotes).
    response = admin_client.post(
        f"/quotes/r/{revision.pk}/estimate/laser/",
        {**data, "manual_price": "12", "price_note": "Bulk order", "add": "1"},
    )
    assert response.status_code == 302, response.content[:500]
    line = revision.lines.get()
    assert line.unit_price == D("12") and line.calculated_unit_price == D("16.0000")
    assert line.line_total == D("120.00")
    assert sum(D(v) for v in line.split.values()) == D("120.00")
    assert line.split == {"materials": "30.00", "machine": "67.50", "labor": "22.50"}
    page = admin_client.get(revision.get_absolute_url()).content.decode()
    assert "Manual price" in page and "Bulk order" in page
    # The reason is internal: the customer's document never shows it.
    revision.refresh_from_db()
    assert "Bulk order" not in services.render_html(revision)


@pytest.mark.django_db
def test_add_buttons_come_before_the_lines(admin_client, revision):  # noqa: F811
    page = admin_client.get(revision.get_absolute_url()).content.decode()
    assert page.index('class="add-lines"') < page.index('<table class="lines"')


def test_estimators_are_unchanged_by_the_price_form():
    assert "manual_price" not in ESTIMATORS["laser"].form_class()().fields
