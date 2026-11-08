"""FR-PRD-2: a one-off quote or sale line can be kept as a catalog product."""

from decimal import Decimal

import pytest

from tests.conftest import make_user
from tests.test_billing import draft, taxes  # noqa: F401 — fixtures
from trammel.catalog.models import Product
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes

D = Decimal
LINE = {
    "description": "Engraved coaster set (4)\nWalnut, laser engraved",
    "quantity": "120",
    "unit": "each",
    "unit_price": "21.60",
    "discount_pct": "0",
}


@pytest.mark.django_db
def test_sale_line_saved_to_the_catalog_once(admin_client, draft):  # noqa: F811
    url = f"/invoices/{draft.pk}/lines/"
    page = admin_client.get(f"/invoices/{draft.pk}/").content.decode()
    assert 'name="save_to_catalog"' in page
    response = admin_client.post(url, {**LINE, "save_to_catalog": "on"}, follow=True)
    assert "Added “Engraved coaster set (4)” to the catalog." in response.content.decode()
    product = Product.objects.get()
    assert product.price == D("21.60") and product.unit == "each"
    assert product.description == "Engraved coaster set (4)\nWalnut, laser engraved"
    line = draft.lines.get(description__startswith="Engraved")
    assert line.product == product and line.quantity == D("120")

    response = admin_client.post(url, {**LINE, "save_to_catalog": "on"}, follow=True)
    assert "already in the catalog" in response.content.decode()
    assert Product.objects.count() == 1


@pytest.mark.django_db
def test_quote_line_saved_to_the_catalog(admin_client, customer):
    revision = quotes.create_quote(jobs.create_job(Job(title="Coasters", customer=customer)))
    admin_client.post(
        f"/quotes/r/{revision.pk}/lines/new/adhoc/",
        {**LINE, "unit_cost": "6", "save_to_catalog": "on"},
    )
    product = Product.objects.get()
    assert product.name == "Engraved coaster set (4)" and product.cost == D("6")
    assert revision.lines.count() == 1


@pytest.mark.django_db
def test_only_people_who_edit_the_catalog_can_save_to_it(client, draft):  # noqa: F811
    clerk = make_user("clerk@example.com", "billing.view", "billing.create")
    client.force_login(clerk)
    page = client.get(f"/invoices/{draft.pk}/").content.decode()
    assert 'name="save_to_catalog"' not in page
    client.post(f"/invoices/{draft.pk}/lines/", {**LINE, "save_to_catalog": "on"})
    assert not Product.objects.exists()
    assert draft.lines.filter(description__startswith="Engraved").exists()
