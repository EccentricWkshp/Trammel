"""The API end to end, as a client with an API key uses it (FR-API-1).

One job is taken from first quote to a paid invoice, a counter sale is taxed
at a hand-chosen jurisdiction, and then every GET endpoint in the OpenAPI
document is called with the records just made. A listing that only fails at
request time (as GET /jobs/ did in 0.1.0) fails here.
"""

import datetime as dt
import json
import re

import pytest
from django.test import Client

from trammel.accounts.models import ApiKey
from trammel.core import access
from trammel.tax.models import Jurisdiction, TaxRate


@pytest.fixture
def api(admin):
    scopes = sorted(access.permissions_of(access.as_actor(admin)))
    issued = ApiKey.issue(admin, "Test client", scopes)
    token = issued[1] if isinstance(issued, tuple) else issued
    client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")

    def call(method, path, body=None, expect=None):
        response = getattr(client, method.lower())(
            "/api/v1" + path,
            data=json.dumps(body) if body is not None else None,
            content_type="application/json",
        )
        if expect is not None:
            assert response.status_code == expect, (path, response.content[:400])
        return response.json() if response.content else None

    return call


@pytest.mark.django_db
def test_api_flow_and_every_get_endpoint(api):
    vendor = api("POST", "/shop/vendors", {"name": "Cascade Hardwoods"}, expect=201)
    material = api(
        "POST",
        "/shop/materials",
        {
            "name": "Walnut 8/4",
            "category": "lumber",
            "vendor_id": vendor["id"],
            "unit": "board_ft",
            "unit_cost": "14.50",
            "markup": "25",
        },
        expect=201,
    )
    machine = api(
        "POST",
        "/shop/machines",
        {"name": "Laser", "kind": "laser", "hourly_rate": "45.00"},
        expect=201,
    )
    product = api(
        "POST", "/catalog/", {"name": "Coaster set", "kind": "made", "price": "48.00"}, expect=201
    )
    customer = api("POST", "/customers/", {"kind": "company", "name": "Northline"}, expect=201)
    walk_in = api("POST", "/customers/", {"name": "Tom Becker"}, expect=201)

    job = api("POST", "/jobs/", {"title": "Table", "customer": customer["id"]}, expect=201)
    revision = api("POST", "/quotes/", {"job": job["id"], "title": "Table"}, expect=201)
    for kind, data in [
        ("material", {"material": material["id"], "quantity": "10"}),
        ("labor", {"description": "Shop labor", "quantity": "4"}),
        ("machine", {"machine": machine["id"], "quantity": "1"}),
        ("product", {"product": product["id"], "quantity": "2"}),
        (
            "adhoc",
            {"description": "Steel base", "quantity": "1", "unit": "each", "unit_price": "640.00"},
        ),
    ]:
        api("POST", f"/quotes/{revision['id']}/lines", {"kind": kind, "data": data}, expect=201)
    api("POST", f"/quotes/{revision['id']}/send", expect=200)
    api(
        "POST",
        f"/quotes/{revision['id']}/approve",
        {"approved_by_name": "Priya", "how": "email", "accept_agreement": True},
        expect=200,
    )

    wo = api("POST", "/work-orders/", {"job": job["id"], "revision": revision["id"]}, expect=201)
    api("POST", f"/work-orders/{wo['id']}/transition", {"target": "in_progress"}, expect=200)
    started = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    entry = api(
        "POST",
        "/time/manual",
        {"work_order": wo["id"], "started_at": started.isoformat(), "minutes": "90"},
        expect=201,
    )
    api("POST", f"/work-orders/{wo['id']}/notes", {"text": "Glued up"}, expect=201)

    # A counter sale whose rate cannot be looked up is taxed at a jurisdiction
    # chosen by hand, and flagged for review once issued.
    place = Jurisdiction.objects.create(code="9999", name="Hometown", region="WA")
    TaxRate.objects.create(jurisdiction=place, rate="8.5", effective_from=dt.date(2020, 1, 1))
    sale = api("POST", "/invoices/", {"customer": walk_in["id"], "kind": "direct_sale"}, expect=201)
    api(
        "POST",
        f"/invoices/{sale['id']}/lines",
        {"description": "Coaster set", "quantity": "1", "unit_price": "48.00"},
        expect=201,
    )
    found = api("GET", "/tax/jurisdictions?q=hometown", expect=200)
    assert [j["code"] for j in found["items"]] == ["9999"]
    patched = api(
        "PATCH",
        f"/invoices/{sale['id']}",
        {"manual_jurisdiction": found["items"][0]["id"]},
        expect=200,
    )
    assert patched["manual_jurisdiction"] == str(place.pk)
    issued = api("POST", f"/invoices/{sale['id']}/issue", expect=200)
    assert issued["tax_flagged"] is True and issued["tax_total"] == "4.08"
    api(
        "POST",
        f"/invoices/{sale['id']}/payments",
        {"amount": issued["total"], "received_on": dt.date.today().isoformat(), "method": "card"},
        expect=200,
    )

    ids = {
        "customer_id": customer["id"],
        "job_id": job["id"],
        "revision_id": revision["id"],
        "wo_id": wo["id"],
        "invoice_id": sale["id"],
        "entry_id": entry["id"],
        "label": "jobs.job",
        "object_id": job["id"],
    }
    required_query = {"/api/v1/tax/lookup": "?postal_code=98501"}
    # The schema URL needs a signed-in session (docs_decorator), not a key.
    from trammel.api.api import api as ninja_api

    schema = ninja_api.get_openapi_schema(path_prefix="/api/v1/")
    checked = 0
    for path, operations in schema["paths"].items():
        if "get" not in operations:
            continue
        url = re.sub(r"\{(\w+)\}", lambda m: ids[m.group(1)], path)
        url += required_query.get(path, "")
        response = api("GET", url.removeprefix("/api/v1"), expect=200)
        assert response is not None
        checked += 1
    assert checked >= 19
