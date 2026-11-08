"""FR-RPT-1..8 and FR-JOB-3, on one end-to-end job."""

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from trammel.billing import services as billing
from trammel.core.models import BusinessProfile, ShopRates
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.reports import services
from trammel.tax.models import Boundary, Jurisdiction, TaxCategory, TaxRate
from trammel.timetracking import services as time
from trammel.workorders import services as wos

D = Decimal


@pytest.fixture
def world(setup_done, customer, admin):
    business = BusinessProfile.load()
    business.postal_code = "98402"
    business.save()
    rates = ShopRates.load()
    rates.labor_hourly_rate = D("60")
    rates.save()
    wa = Jurisdiction.objects.create(code="2717", name="Tacoma")
    TaxRate.objects.create(jurisdiction=wa, rate=D("10"), effective_from=dt.date(2020, 1, 1))
    TaxRate.objects.create(
        jurisdiction=wa,
        kind="gross_receipts",
        classification="Retailing",
        rate=D("0.471"),
        effective_from=dt.date(2020, 1, 1),
    )
    Boundary.objects.create(
        zip_low="984020000",
        zip_high="984029999",
        jurisdiction_codes=["2717"],
        location_code="2717",
        source="manual",
    )
    TaxCategory.objects.create(
        name="Retail", taxable=True, is_default=True, gross_receipts_classification="Retailing"
    )
    job = jobs.create_job(Job(title="Table", customer=customer))
    rev = quotes.create_quote(job)
    form = LINE_FORMS["labor"]({"description": "Build", "quantity": "10"})
    assert form.is_valid()
    quotes.save_line(form.build(rev))
    quotes.send(rev, actor=admin)
    quotes.approve(rev, actor=admin, approved_by_name="G", accept_agreement=True, user=admin)
    rev.refresh_from_db()
    wo = wos.create(job, revision=rev)
    time.manual(wo, kind="labor", minutes=D("480"), started_at=timezone.now(), user=admin)
    wos.record_material(
        wo,
        material=None,
        description="Walnut",
        quantity=D("1"),
        unit="each",
        unit_cost=D("200"),
        user=admin,
    )
    invoice = billing.create(customer, kind="final", job=job)
    billing.add_from_quote(invoice, rev)
    invoice = billing.issue(invoice, actor=admin)
    return job, invoice


@pytest.mark.django_db
def test_job_costing(world):
    job, _invoice = world
    c = services.job_costing(job)
    assert c.quoted["labor"] == D("600.00") and c.quoted_total == D("600.00")
    assert c.actual["labor"] == D("480.00") and c.actual["materials"] == D("200.00")
    assert c.invoiced == D("600.00") and c.margin == D("-80.00")


@pytest.mark.django_db
def test_sales_and_aging(world, customer, admin):
    _job, invoice = world
    today = timezone.localdate()
    rows, total = services.sales(today, today, "customer")
    assert rows == [("Grace Hopper", D("600.00"))] and total == D("600.00")
    note = billing.issue(
        billing.create_credit_note(invoice, amount=D("100"), description="x"), actor=admin
    )
    assert note.number
    _rows, total = services.sales(today, today, "month")
    assert total == D("500.00")
    rows, totals = services.ar_aging(today + dt.timedelta(days=45))
    assert rows[0]["d1_30"] == D("560.00")  # 660 less the 100 credit; due in 30 days
    assert totals["total"] == D("560.00")


@pytest.mark.django_db
def test_tax_report_with_bo_and_bad_debt(world, admin):
    _job, invoice = world
    billing.write_off(invoice, amount=D("60"), reason="Dispute", user=admin)
    today = timezone.localdate()
    data = services.tax_report(today, today)
    row = data["rows"][0]
    assert row["code"] == "2717" and row["gross"] == D("600.00") and row["tax"] == D("60.00")
    retail = next(b for b in data["bo"] if b["classification"] == "Retailing")
    assert retail["basis"] == D("600.00") and retail["tax"] == D("2.83")
    assert data["bad_debt"] == D("60.00")


@pytest.mark.django_db
def test_deposits_count_once(setup_done, customer, admin):
    rates = ShopRates.load()
    rates.labor_hourly_rate = D("100")
    rates.save()
    customer.tax_exempt, customer.tax_exempt_number = True, "RS-1"  # tax is not the point here
    customer.save()
    job = jobs.create_job(Job(title="Bench", customer=customer))
    rev = quotes.create_quote(job)
    form = LINE_FORMS["labor"]({"description": "Build", "quantity": "10"})
    assert form.is_valid()
    quotes.save_line(form.build(rev))
    rev.deposit_kind, rev.deposit_value = "fixed", D("400")
    quotes.update_revision(rev, before={})
    quotes.send(rev, actor=admin)
    quotes.approve(rev, actor=admin, approved_by_name="G", accept_agreement=True, user=admin)
    deposit = job.invoices.get(kind="deposit")
    billing.record_payment(
        customer,
        amount=D("400"),
        received_on=timezone.localdate(),
        method="cash",
        allocations={deposit: D("400")},
    )
    final = billing.create(customer, kind="final", job=job)
    billing.add_from_quote(final, rev)
    billing.add_deposit_credits(final)
    billing.issue(final, actor=admin)
    assert services.job_costing(job).invoiced == D("1000.00")
    today = timezone.localdate()
    assert services.tax_report(today, today)["rows"][0]["gross"] == D("1000.00")
    assert services.sales(today, today)[1] == D("1000.00")


@pytest.mark.django_db
def test_time_report(world):
    today = timezone.localdate()
    rows, _ = services.time_report(today, today, "user")
    assert rows[0]["hours"] == D("8.00")


@pytest.mark.django_db
def test_force_closed_report(world, admin):
    job, invoice = world
    jobs.force_close(
        job, actor=admin, reason="Gone", resolutions={f"invoice:{invoice.pk}": "write_off"}
    )
    rows, write_offs = services.force_closed()
    assert rows[0]["job"] == job and rows[0]["outstanding"]
    assert write_offs.get().from_force_closeout


@pytest.mark.django_db
def test_report_screens_and_csv(admin_client, world):
    job, _ = world
    for path in (
        "/reports/",
        "/reports/profitability/",
        "/reports/sales/?group=product",
        "/reports/aging/",
        "/reports/tax/",
        "/reports/time/?group=machine",
        "/reports/force-closed/",
        f"/jobs/{job.pk}/",
    ):
        assert admin_client.get(path).status_code == 200, path
    csv = admin_client.get("/reports/tax/?format=csv")
    body = b"".join(csv.streaming_content).decode()
    assert "2717" in body and "Bad debt" in body
    assert "Job costing" in admin_client.get(f"/jobs/{job.pk}/").content.decode()
