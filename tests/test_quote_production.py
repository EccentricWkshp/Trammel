"""Quotes and the schedule (0.8.2): each line's production time, carried to the
work order; when the schedule could finish a quote if approved now, and
promising that date; and the rush fee on a rush job's quotes."""

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from trammel.catalog.models import Product, RoutingStep
from trammel.core.models import ShopRates
from trammel.jobs import services as jobs
from trammel.jobs.models import Job, Priority
from trammel.quotes import production
from trammel.quotes import services as quotes
from trammel.quotes.estimators import ESTIMATORS
from trammel.quotes.forms import LINE_FORMS
from trammel.quotes.models import QuoteLine
from trammel.shop.models import Machine
from trammel.workorders import scheduling
from trammel.workorders import services as wos

from .test_scheduling import MONDAY, _wall, _work_order, shop  # noqa: F401

D = Decimal


def _line(revision, kind, **data):
    form = LINE_FORMS[kind](data)
    assert form.is_valid(), form.errors
    return quotes.save_line(form.build(revision))


def _job(customer, priority=Priority.NORMAL, **dates):
    return jobs.create_job(Job(title="Sign", customer=customer, priority=priority, **dates))


def _approve(revision, admin):
    quotes.send(revision, actor=admin)
    revision.refresh_from_db()
    quotes.approve(
        revision,
        actor=admin,
        approved_by_name="Pat",
        how="email",
        accept_agreement=True,
        user=admin,
    )
    revision.refresh_from_db()


@pytest.fixture
def laser(db):
    return Machine.objects.create(name="Laser", kind="laser", hourly_rate=D("60"))


@pytest.mark.django_db
def test_each_lines_production_time(customer, laser):
    revision = quotes.create_quote(_job(customer))
    _line(revision, "labor", description="Assembly", quantity="2.5")
    _line(revision, "machine", machine=str(laser.pk), quantity="1.5")
    _line(revision, "adhoc", description="Hardware", quantity="1", unit="each", unit_price="20")
    product = Product.objects.create(sku="BOX", name="Box", kind="made", price=D("40"))
    RoutingStep.objects.create(
        product=product,
        position=1,
        name="Cut",
        machine=laser,
        setup_minutes=D("10"),
        machine_minutes=D("6"),
        labor_minutes=D("2"),
    )
    RoutingStep.objects.create(
        product=product, position=2, name="Glue", labor_minutes=D("5"), wait_hours=D("12")
    )
    _line(revision, "product", product=str(product.pk), quantity="3")
    estimator = ESTIMATORS["laser"]
    form = estimator.form_class()(
        {
            "description": "Engraved sign",
            "quantity": "2",
            "machine": str(laser.pk),
            "cut_minutes": "6",
            "engrave_minutes": "24",
            "labor_minutes": "15",
            "setup_minutes": "30",
            "area_unit": "sq_in",
        }
    )
    assert form.is_valid(), form.errors
    quotes.save_estimate(quotes.estimate_line(revision, "laser", form.cleaned_data))
    times = {t.line.description: t for t in production.for_revision(revision)}
    assert times["Assembly"].labor == D("150") and not times["Assembly"].run
    machine = times[f"Machine time — {laser}"]
    assert machine.run == D("90") and machine.steps[0].machine == laser
    assert not times["Hardware"].has_time
    box = times["Box"]
    assert box.routed and [s.name for s in box.steps] == ["Cut", "Glue"]
    assert (box.labor, box.run, box.wait) == (D("10") + 3 * D("2") + 3 * D("5"), D("18"), D("12"))
    sign = times["Engraved sign"]
    assert sign.run == D("60") and sign.labor == D("60") and sign.steps[0].machine == laser
    # Breakdown rows from before 0.8.2 carry their hours only in the detail text.
    line = sign.line
    line.breakdown = [
        {k: v for k, v in r.items() if k not in ("hours", "machine")} for r in line.breakdown
    ]
    assert production.for_line(line).run == D("60")


@pytest.mark.django_db
def test_the_work_order_plans_the_quotes_time(customer, laser, admin):
    revision = quotes.create_quote(_job(customer))
    _line(revision, "labor", description="Assembly", quantity="2")
    _line(revision, "machine", machine=str(laser.pk), quantity="0.5")
    _approve(revision, admin)
    wo = wos.create(revision.quote.job, revision=revision)
    ops = {op.name: op for op in wo.operations.all()}
    assert ops["Assembly"].planned_labor_minutes == D("120")
    machine = ops[f"Machine time — {laser}"]
    assert machine.planned_machine_minutes == D("30") and machine.machine == laser


@pytest.mark.django_db
def test_when_the_schedule_could_finish_a_quote(shop, admin, admin_client, customer):  # noqa: F811
    # One person, 8–4 weekdays: a day of work ahead in the queue.
    _work_order(admin, "Ahead", (480, 0, None, 0), priority=Priority.HIGH)
    _work_order(admin, "Behind", (120, 0, None, 0), priority=Priority.LOW)
    job = _job(customer, promised_date=dt.date(2026, 11, 16))
    revision = quotes.create_quote(job)
    _line(revision, "labor", description="Assembly", quantity="2")
    now = workhours_now()
    plan = scheduling.forecast(revision, now=now)
    # Behind the high-priority day, ahead of the low-priority work.
    assert plan.position == 2 and _wall(plan.finish) == MONDAY.replace(day=17, hour=10)
    assert plan.late == ("the job's promised date", dt.date(2026, 11, 16))
    rush = scheduling.forecast(revision, rush=True, now=now)
    assert rush.position == 1 and _wall(rush.finish) == MONDAY.replace(hour=10)
    # Nothing was saved.
    assert not wos.WorkOrder.objects.filter(job=job).exists()

    page = admin_client.get(f"{revision.get_absolute_url()}?tab=production").content.decode()
    assert "Production time" in page and "If it were approved now" in page
    # The page forecasts from the real time of day: rush is offered when it'd
    # finish sooner than the normal forecast, as from MONDAY above.
    real, real_rush = scheduling.forecast(revision), scheduling.forecast(revision, rush=True)
    sooner = real_rush.finish and real.finish and real_rush.finish < real.finish
    assert ("As a rush job it could finish by" in page) == bool(sooner)
    # Offered unless the forecast is the date already promised.
    offered = timezone.localtime(real.finish).date() != job.promised_date
    assert ("Promise " in page) == offered
    day = scheduling.forecast(revision).finish.date().isoformat()
    response = admin_client.post(f"/work-orders/quote-promise/{revision.pk}/", {"date": day})
    assert response.status_code == 302
    job.refresh_from_db()
    assert job.promised_date.isoformat() == day
    body = admin_client.get(f"/api/v1/work-orders/forecast/{revision.pk}").json()
    assert body["labor_minutes"] == "120" or float(body["labor_minutes"]) == 120
    assert body["queue_position"] == 2 and body["rush_finish"] is not None


def workhours_now():
    from trammel.core import workhours

    return workhours.Hours.load().aware(MONDAY)


@pytest.fixture
def rush_rates(db):
    rates = ShopRates.load()
    rates.rush_fee_kind, rates.rush_fee_value, rates.rush_fee_minimum = "percent", D("25"), D("50")
    rates.save()
    return rates


def _rush_line(revision):
    return revision.lines.filter(kind=QuoteLine.Kind.RUSH).first()


@pytest.mark.django_db
def test_a_rush_jobs_quote_gets_a_rush_fee(customer, rush_rates, admin):
    job = _job(customer, priority=Priority.RUSH)
    revision = quotes.create_quote(job)
    fee = _rush_line(revision)
    assert fee.description == "Rush fee (25%)" and fee.line_total == 0  # nothing to rush yet
    _line(revision, "adhoc", description="Sign", quantity="1", unit="each", unit_price="100")
    fee.refresh_from_db()
    assert fee.line_total == D("50")  # 25 % is 25: the minimum
    _line(revision, "adhoc", description="Stand", quantity="1", unit="each", unit_price="300")
    fee.refresh_from_db()
    revision.refresh_from_db()
    assert fee.line_total == D("100") and revision.lines_total == D("500")
    # The fee stays last, after lines added later.
    assert list(revision.lines.values_list("description", flat=True))[-1] == "Rush fee (25%)"
    # A flat fee, from the quote's own rates.
    before = quotes.audit.snapshot(revision)
    revision.rush_fee_kind, revision.rush_fee_value = "flat", D("75")
    quotes.update_revision(revision, before=before)
    fee.refresh_from_db()
    assert fee.line_total == D("75") and revision.rates_overridden
    # A price typed in sticks.
    form = LINE_FORMS["rush"](
        {"description": "Rush fee", "manual_price": "40", "price_note": "Regular customer"}
    )
    assert form.is_valid(), form.errors
    built = form.build(revision, fee)
    quotes.save_line(built)
    _line(revision, "adhoc", description="Bracket", quantity="1", unit="each", unit_price="50")
    fee.refresh_from_db()
    assert fee.line_total == D("40")


@pytest.mark.django_db
def test_the_rush_fee_follows_the_jobs_priority_and_the_customers_choices(
    customer, rush_rates, admin
):
    job = _job(customer)
    revision = quotes.create_quote(job)
    assert _rush_line(revision) is None
    _line(revision, "adhoc", description="Build", quantity="1", unit="each", unit_price="1000")
    option = _line(
        revision, "adhoc", description="Bench", quantity="1", unit="each", unit_price="400"
    )
    quotes.set_offer(option, "optional")
    before = quotes.audit.snapshot(job)
    job.priority = Priority.RUSH
    jobs.update_job(job, before=before)
    fee = _rush_line(revision)
    assert fee is not None and fee.line_total == D("250")
    # A sent quote isn't touched; the customer adding the option raises the fee.
    quotes.send(revision, actor=admin)
    revision.refresh_from_db()
    context = quotes.document_context(revision, picks=[str(option.pk)])
    rows = {r["description"]: r["total"] for r in context["quote"]["lines"]}
    assert rows["Rush fee (25%)"] == D("350") and context["quote"]["total"] >= D("1750")
    quotes.approve(
        revision,
        actor=admin,
        approved_by_name="Pat",
        how="email",
        accept_agreement=True,
        user=admin,
        picks=[str(option.pk)],
    )
    fee.refresh_from_db()
    revision.refresh_from_db()
    assert fee.line_total == D("350") and revision.lines_total == D("1750")
    # It's not work: no operation for it.
    job.refresh_from_db()
    wo = wos.create(job, revision=revision)
    assert not wo.operations.filter(name__startswith="Rush fee").exists()
    # Back from Rush, a new draft loses it.
    other = quotes.create_quote(job)
    assert _rush_line(other) is not None
    before = quotes.audit.snapshot(job)
    job.priority = Priority.NORMAL
    jobs.update_job(job, before=before)
    assert _rush_line(other) is None and _rush_line(revision) is not None


@pytest.mark.django_db
def test_marking_a_quote_rush_makes_the_job_rush_when_approved(
    customer, rush_rates, admin, admin_client
):
    from trammel.core.models import AuditEvent

    job = _job(customer)
    revision = quotes.create_quote(job)
    _line(revision, "labor", description="Build", quantity="4")
    _line(revision, "adhoc", description="Sign", quantity="1", unit="each", unit_price="400")
    response = admin_client.post(f"/quotes/r/{revision.pk}/rush/", {"rush": "1"})
    assert response.status_code == 302
    fee = _rush_line(revision)
    assert fee.line_total == D("100")
    page = admin_client.get(revision.get_absolute_url()).content.decode()
    assert "rush job" in page and "Not a rush job" in page
    page = admin_client.get(f"{revision.get_absolute_url()}?tab=production").content.decode()
    assert "carries the rush fee" in page
    _approve(revision, admin)
    job.refresh_from_db()
    assert job.priority == Priority.RUSH
    assert AuditEvent.objects.filter(action="job.rush").exists()


@pytest.mark.django_db
def test_rush_offered_as_an_option_is_the_customers_choice(customer, rush_rates, admin):
    def offered():
        job = _job(customer)
        revision = quotes.create_quote(job)
        _line(revision, "adhoc", description="Sign", quantity="1", unit="each", unit_price="400")
        quotes.set_rush(revision, True)
        fee = _rush_line(revision)
        quotes.set_offer(fee, "optional")
        quotes.send(revision, actor=admin)
        revision.refresh_from_db()
        return job, revision, fee

    job, revision, fee = offered()
    context = quotes.document_context(revision)
    assert [c["description"] for c in context["quote"]["choices"]] == ["Rush fee (25%)"]
    quotes.approve(
        revision,
        actor=admin,
        approved_by_name="Pat",
        how="email",
        accept_agreement=True,
        user=admin,
    )
    job.refresh_from_db()
    assert job.priority == Priority.NORMAL  # not chosen
    job, revision, fee = offered()
    quotes.approve(
        revision,
        actor=admin,
        approved_by_name="Pat",
        how="email",
        accept_agreement=True,
        user=admin,
        picks=[str(fee.pk)],
    )
    job.refresh_from_db()
    revision.refresh_from_db()
    assert job.priority == Priority.RUSH and revision.lines_total == D("500")
    # And it can be taken off a draft again.
    other = quotes.create_quote(_job(customer))
    quotes.set_rush(other, True)
    quotes.set_rush(other, False)
    assert _rush_line(other) is None
