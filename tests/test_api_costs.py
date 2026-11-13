"""Costs in the API follow the same rule as the pages (SEC-03, 0.10.5): without
"See costs and margins" (core.see_costs), cost fields are null or left out, for
a signed-in browser and an API key alike."""

import json
from decimal import Decimal

import pytest
from django.test import Client

from tests.conftest import make_user
from trammel.accounts.models import ApiKey
from trammel.catalog import services as catalog
from trammel.catalog.models import Product
from trammel.shop.models import Material

D = Decimal


def _key_client(user, scopes):
    issued = ApiKey.issue(user, "Test", scopes)
    token = issued[1] if isinstance(issued, tuple) else issued
    return Client(HTTP_AUTHORIZATION=f"Bearer {token}")


@pytest.mark.django_db
def test_costs_hidden_without_permission(setup_done):
    catalog.save_product(Product(name="Cutting board", price=D("100"), cost_override=D("42")))
    Material.objects.create(name="Walnut", unit="board_ft", unit_cost=D("14.50"))

    reader = make_user("reader@example.com", "catalog.view", "shop.view")
    session = Client()
    session.force_login(reader)
    for client in (session, _key_client(reader, ["catalog.view", "shop.view"])):
        product = client.get("/api/v1/catalog/").json()["items"][0]
        assert product["price"] is not None
        assert product["cost"] is None and product["cost_override"] is None
        assert product["other_cost"] is None and product["cost_breakdown"] == []
        materials = client.get("/api/v1/shop/materials").json()
        rows = materials["items"] if isinstance(materials, dict) else materials
        assert rows and all(m["unit_cost"] is None for m in rows)

    costs = make_user("costs@example.com", "catalog.view", "shop.view", "core.see_costs")
    session = Client()
    session.force_login(costs)
    product = session.get("/api/v1/catalog/").json()["items"][0]
    assert D(product["cost"]) == D("42") and D(product["cost_override"]) == D("42")
    # A key gets costs only when it's given them, even from someone who has them.
    keyed = _key_client(costs, ["catalog.view"]).get("/api/v1/catalog/").json()["items"][0]
    assert keyed["cost"] is None
    keyed = _key_client(costs, ["catalog.view", "core.see_costs"])
    assert D(keyed.get("/api/v1/catalog/").json()["items"][0]["cost"]) == D("42")


@pytest.mark.django_db
def test_quote_breakdowns_drop_costs_without_permission(setup_done, customer):
    from trammel.jobs import services as jobs
    from trammel.jobs.models import Job
    from trammel.quotes import services as quotes
    from trammel.quotes.models import QuoteLine

    job = jobs.create_job(Job(title="Sign", customer=customer))
    rev = quotes.create_quote(job)
    QuoteLine.objects.create(
        revision=rev,
        position=1,
        kind="estimate",
        description="Engraved sign",
        quantity=D("1"),
        unit_price=D("180"),
        line_total=D("180"),
        estimator="laser",
        breakdown=[{"label": "Machine", "detail": "35 min", "cost": "22.10", "price": "40.00"}],
    )
    reader = make_user("quotes@example.com", "quotes.view", "jobs.view")
    client = Client()
    client.force_login(reader)
    line = client.get(f"/api/v1/quotes/{rev.pk}").json()["lines"][0]
    assert line["breakdown"] == [{"label": "Machine", "detail": "35 min", "price": "40.00"}]
    assert "22.10" not in json.dumps(line)
