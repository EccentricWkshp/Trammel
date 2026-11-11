"""FR-TAX: rates with effective dates, lookup by address, sourcing, rounding, imports."""

import datetime as dt
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from trammel.tax import importers, services
from trammel.tax.models import Boundary, Jurisdiction, TaxRate, TaxSettings

D = Decimal
JAN, JUL = dt.date(2026, 1, 15), dt.date(2026, 7, 15)


@pytest.fixture
def tacoma(setup_done):
    j = Jurisdiction.objects.create(code="2717", name="Tacoma", region="WA")
    TaxRate.objects.create(
        jurisdiction=j,
        rate=D("10.30"),
        effective_from=dt.date(2025, 4, 1),
        effective_to=dt.date(2026, 3, 31),
    )
    TaxRate.objects.create(jurisdiction=j, rate=D("10.40"), effective_from=dt.date(2026, 4, 1))
    Boundary.objects.create(
        zip_low="984020000",
        zip_high="984099999",
        jurisdiction_codes=["2717"],
        location_code="2717",
        source="manual",
    )
    return j


@pytest.mark.django_db
def test_rate_history_by_date(tacoma):
    assert services.lookup_postal("98402", JAN).total_rate == D("10.30")
    assert services.lookup_postal("98402-1234", JUL).total_rate == D("10.40")


@pytest.mark.django_db
def test_narrowest_boundary_wins(tacoma):
    special = Jurisdiction.objects.create(code="2799", name="Port district")
    TaxRate.objects.create(jurisdiction=special, rate=D("9.9"), effective_from=dt.date(2020, 1, 1))
    Boundary.objects.create(
        zip_low="984021000",
        zip_high="984021999",
        jurisdiction_codes=["2799"],
        location_code="2799",
        source="manual",
    )
    assert services.lookup_postal("98402-1500", JUL).location_code == "2799"
    assert services.lookup_postal("98402-2500", JUL).location_code == "2717"


@pytest.mark.django_db
def test_unresolved_addresses_say_why(tacoma):
    result = services.lookup_postal("10001", JUL)
    assert not result.resolved and "No tax boundary" in result.note
    assert not services.lookup_postal("ABC", JUL).resolved


@pytest.mark.django_db
def test_sourcing_rule(tacoma):
    seattle = Jurisdiction.objects.create(code="1726", name="Seattle")
    TaxRate.objects.create(
        jurisdiction=seattle, rate=D("10.35"), effective_from=dt.date(2020, 1, 1)
    )
    Boundary.objects.create(
        zip_low="981010000", zip_high="981019999", jurisdiction_codes=["1726"], source="manual"
    )
    kw = {"destination_postal": "98101", "shop_postal": "98402", "on": JUL}
    assert services.determine(fulfillment="delivery", **kw).total_rate == D("10.35")
    assert services.determine(fulfillment="pickup", **kw).total_rate == D("10.40")
    settings = TaxSettings.load()
    settings.sourcing = "origin"
    settings.save()
    assert services.determine(fulfillment="shipping", **kw).total_rate == D("10.40")


@pytest.mark.django_db
def test_tax_rounds_half_up_per_rate(setup_done):
    det = services.Determination(
        True,
        components=[
            services.Component("ST", "State", D("6.5")),
            services.Component("LOC", "Local", D("3.9")),
        ],
    )
    lines = services.calculate(D("10.10"), det)
    # 0.6565 → 0.66 and 0.3939 → 0.39, each rounded on its own.
    assert [line.amount for line in lines] == [D("0.66"), D("0.39")]
    assert services.calculate(
        D("0.50"), services.Determination(True, components=[services.Component("X", "X", D("5"))])
    )[0].amount == D("0.03")


@pytest.mark.django_db
def test_generic_csv_import(setup_done):
    data = (
        b"postal_code,jurisdiction_code,jurisdiction_name,rate,effective_from,effective_to\n"
        b"98101,1726,Seattle,10.35,2025-04-01,\n"
        b"98402-98409,2717,Tacoma,10.30,2025-04-01,\n"
        b"bogus,9999,Bad,1,2025-01-01,\n"
    )
    batch = importers.import_generic_csv(data, filename="rates.csv")
    assert (batch.rows, batch.skipped) == (2, 1)
    assert services.lookup_postal("98405", JUL).total_rate == D("10.30")
    assert services.lookup_postal("98101-0001", JUL).location_code == "1726"


@pytest.mark.django_db
def test_sst_rate_and_boundary_import(setup_done):
    rates = (
        b"53,45,53,0.065000,0.065000,0.000000,0.000000,20090401,29991231\n"
        b"53,00,053,0.000000,0.000000,0.000000,0.000000,20090401,29991231\n"
        b"53,01,70000,0.039000,0.039000,0.000000,0.000000,20250401,29991231\n"
    )
    importers.import_sst_rates(rates, filename="WAR.csv")
    fields = [""] * 32
    fields[0], fields[1], fields[2] = "Z", "20250401", "29991231"
    fields[17], fields[19] = "98402", "98402"
    fields[21], fields[22], fields[24], fields[25] = "2717", "53", "053", "70000"
    address_record = "A," + ",".join([""] * 30)
    boundaries = (",".join(fields) + "\n" + address_record + "\n").encode()
    batch = importers.import_sst_boundaries(boundaries, filename="WAB.csv")
    assert (batch.rows, batch.skipped) == (1, 1)
    result = services.lookup_postal("98402", JUL)
    assert result.resolved and result.location_code == "2717"
    assert result.total_rate == D("10.4")


@pytest.mark.django_db
def test_disabled_source_is_ignored(tacoma):
    importers.import_generic_csv(
        b"postal_code,jurisdiction_code,rate,effective_from\n10001,NYC,8.875,2020-01-01\n",
        filename="ny.csv",
    )
    assert services.lookup_postal("10001", JUL).resolved
    settings = TaxSettings.load()
    settings.csv_enabled = False
    settings.save()
    assert not services.lookup_postal("10001", JUL).resolved


@pytest.mark.django_db
def test_tax_screens(admin_client, tacoma):
    assert admin_client.get("/tax/?postal_code=98402").status_code == 200
    assert "10.4" in admin_client.get("/tax/?postal_code=98402").content.decode()
    for path in (
        "/tax/import/",
        "/tax/settings/",
        "/tax/rates/new/",
        "/tax/boundaries/new/",
        "/tax/categories/new/",
        "/tax/jurisdictions/new/",
    ):
        assert admin_client.get(path).status_code == 200, path
    upload = SimpleUploadedFile(
        "r.csv", b"postal_code,jurisdiction_code,rate,effective_from\n99201,SPK,9.1,2024-01-01\n"
    )
    response = admin_client.post("/tax/import/", {"kind": "csv", "file": upload})
    assert response.status_code == 302
    assert services.lookup_postal("99201", JUL).resolved


@pytest.mark.django_db
def test_catalog(admin_client):
    from trammel.catalog.models import Product

    response = admin_client.post(
        "/products/new/",
        {
            "name": "Cutting board",
            "sku": "CB-1",
            "kind": "made",
            "price": "45",
            "cost_override": "12",
            "unit": "each",
            "is_active": "on",
        },
    )
    assert response.status_code == 302
    dup = admin_client.post(
        "/products/new/",
        {
            "name": "Other",
            "sku": "CB-1",
            "kind": "made",
            "price": "1",
            "cost": "0",
            "unit": "each",
        },
    )
    assert "already has this SKU" in dup.content.decode()
    assert Product.objects.count() == 1
    assert "Cutting board" in admin_client.get("/products/?q=board").content.decode()
