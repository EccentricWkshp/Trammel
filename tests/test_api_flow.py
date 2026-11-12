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
        if response.get("Content-Type", "").startswith("application/json"):
            return response.json()
        # A file's bytes.
        return b"".join(response.streaming_content) if response.streaming else response.content

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

    # Buy more walnut: a purchase order placed and received at landed cost.
    po = api(
        "POST", "/purchasing/orders", {"vendor": vendor["id"], "shipping": "12.00"}, expect=201
    )
    line = api(
        "POST",
        f"/purchasing/orders/{po['id']}/lines",
        {"material": material["id"], "quantity": "10", "unit_price": "15.00"},
        expect=201,
    )
    placed = api("POST", f"/purchasing/orders/{po['id']}/place", expect=200)
    assert placed["number"].startswith("PO-") and placed["total"] == "162.00"
    # Walnut is not kept in stock here, so it is received without a lot.
    received = api(
        "POST",
        f"/purchasing/orders/{po['id']}/receive",
        {"line": line["id"], "quantity": "10"},
        expect=201,
    )
    assert received["value"] == "162.00" and received["lot"] is None
    # A late freight bill once everything is here.
    change = api(
        "POST",
        f"/purchasing/orders/{po['id']}/adjustments",
        {"amount": "8.00", "reason": "Freight bill"},
        expect=201,
    )
    assert change["amount"] == "8.00"

    # A product with a bill of materials, edited, then archived.
    product = api("POST", "/catalog/", {"name": "Serving board", "price": "60"}, expect=201)
    bom = api(
        "POST",
        f"/stock/products/{product['id']}/bom",
        {"material": material["id"], "quantity": "2", "unit": "board_ft"},
        expect=201,
    )
    bom = api(
        "PUT",
        f"/stock/bom/{bom['id']}",
        {"material": material["id"], "quantity": "2.5", "unit": "board_ft", "note": "thick"},
        expect=200,
    )
    assert bom["quantity"] == "2.5" and bom["note"] == "thick"
    archived = api("POST", f"/catalog/{product['id']}/archive", {"archived": True}, expect=200)
    assert archived["is_active"] is False

    ids = {
        "po_id": po["id"],
        "product_id": product["id"],
        "customer_id": customer["id"],
        "job_id": job["id"],
        "revision_id": revision["id"],
        "wo_id": wo["id"],
        "invoice_id": sale["id"],
        "entry_id": entry["id"],
        "label": "jobs.job",
        "object_id": job["id"],
    }
    # The portal on Cloudflare's status, with its module on (not deployed).
    from trammel.core import modules

    modules.set_enabled("portal", True)
    modules.set_enabled("edge", True)
    modules.set_enabled("payments", True)
    ids["event_id"] = api("GET", "/audit/?limit=1", expect=200)["items"][0]["id"]
    # A file the customer sent through the portal, waiting for review.
    from types import SimpleNamespace

    from django.core.files.uploadedfile import SimpleUploadedFile

    from tests.test_api_portal import PNG
    from trammel.customers.models import Customer
    from trammel.portal import uploads
    from trammel.portal.models import PortalSettings

    modules.set_enabled("files", True)
    PortalSettings.objects.update_or_create(pk=1, defaults={"uploads_enabled": True})
    sent, _ = uploads.send(
        SimpleNamespace(customer=Customer.objects.get(pk=customer["id"]), name="Grace"),
        [SimpleUploadedFile("sketch.png", PNG, content_type="image/png")],
    )
    ids["upload_id"] = str(sent[0].pk)
    ids["file_revision_id"] = str(sent[0].document.current_id)
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
