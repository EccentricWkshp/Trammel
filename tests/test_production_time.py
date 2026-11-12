"""Waiting time on routing steps (glue clamping, finish curing) and the production
time estimates that count it: on the product and on the work order."""

import datetime as dt
import re
from decimal import Decimal

import pytest
from django.utils import timezone

from trammel.catalog import services as catalog
from trammel.catalog.models import Product, RoutingStep
from trammel.core.models import ShopRates
from trammel.core.workhours import Hours
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.workorders import services as wos

D = Decimal
UTC = dt.UTC
WEEKDAYS = Hours.same_days(range(5), dt.time(8), dt.time(16), UTC)


def at(day, hour, minute=0):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def test_work_fits_into_shop_hours_and_waiting_goes_around_the_clock():
    # Friday 11/13 at 2 PM: 3 hours of work runs into Monday, then it clamps
    # overnight, and the last hour is done Tuesday morning.
    finish = WEEKDAYS.finish(at(13, 14), [(D("180"), D("12")), (D("60"), D("0"))])
    assert finish == at(17, 9)
    assert WEEKDAYS.working_days_between(at(13, 14), finish) == 3


def test_a_cure_over_the_weekend_lets_monday_start_first_thing():
    finish = WEEKDAYS.finish(at(13, 8), [(D("60"), D("48")), (D("30"), D("0"))])
    assert finish == at(16, 8, 30)
    # Ready before Monday's work starts: Friday is the only working day it takes.
    assert (
        WEEKDAYS.working_days_between(at(13, 8), WEEKDAYS.finish(at(13, 8), [(D("60"), D("48"))]))
        == 1
    )


@pytest.fixture
def board(setup_done):
    rates = ShopRates.load()
    rates.labor_hourly_rate = D("60")
    rates.save()
    product = catalog.save_product(Product(name="Cutting board", kind="made", price=D("85")))
    catalog.save_step(
        RoutingStep(product=product, name="Glue up", labor_minutes=D("60"), wait_hours=D("12"))
    )
    catalog.save_step(
        RoutingStep(product=product, name="Oil", labor_minutes=D("30"), wait_hours=D("24"))
    )
    product.refresh_from_db()
    return product


@pytest.mark.django_db
def test_waiting_adds_time_but_no_cost(admin_client, board):
    assert board.cost == D("90")  # 90 minutes at 60 an hour; the waits are free
    estimate = catalog.production_time(board)
    assert (estimate.work_minutes, estimate.wait_hours) == (D("90"), D("36"))
    assert estimate.finish - estimate.start >= dt.timedelta(hours=37, minutes=30)
    page = admin_client.get(f"{board.get_absolute_url()}?tab=routing").content.decode()
    assert "Production time for a batch of 1" in page
    assert "1 h 30 min of work and 36 hours of waiting" in page
    assert "12 hours" in page


@pytest.fixture
def work_order(board, customer, admin):
    job = jobs.create_job(Job(title="Boards", customer=customer))
    revision = quotes.create_quote(job)
    form = LINE_FORMS["product"]({"product": str(board.pk), "quantity": "2"})
    assert form.is_valid(), form.errors
    quotes.save_line(form.build(revision))
    quotes.send(revision, actor=admin)
    quotes.approve(revision, actor=admin, approved_by_name="Grace", accept_agreement=True)
    job.refresh_from_db()
    return wos.create(job, revision=revision)


@pytest.mark.django_db
def test_a_work_order_gets_the_waits_and_an_estimated_finish(admin_client, work_order, admin):
    glue = work_order.operations.get(name="Glue up")
    assert glue.planned_wait_hours == D("12")
    finish = wos.estimate_finish(work_order)
    assert finish - timezone.now() >= dt.timedelta(hours=36)

    url = f"/work-orders/{work_order.pk}/?tab=operations"
    page = admin_client.get(url).content.decode()
    assert "Scheduled" in page and "Then wait 12 hours" in page
    assert "After the job" not in page
    # The printed traveler has no page styles to space the parts apart.
    traveler = admin_client.get(f"/work-orders/{work_order.pk}/traveler/").content.decode()
    assert "Planned 2 h hands-on. Then wait 12 hours." in re.sub(r"<[^>]+>", "", traveler)

    job = work_order.job
    job.due_date = timezone.localdate()
    job.save()
    assert "After the job&#x27;s due date" in admin_client.get(url).content.decode()

    # Once the glue-up is done, the clamps still hold up the rest for 12 hours.
    wos.set_operation(glue, "done", user=admin)
    glue.refresh_from_db()
    assert wos.estimate_finish(work_order) >= glue.completed_at + dt.timedelta(hours=12)


@pytest.mark.django_db
def test_the_wait_can_be_typed_on_any_operation(admin_client, work_order):
    op = wos.add_operation(work_order, name="Paint")
    admin_client.post(
        f"/work-orders/{work_order.pk}/operations/{op.pk}/edit/",
        {"name": "Paint", "planned_labor_minutes": "20", "planned_wait_hours": "4"},
    )
    op.refresh_from_db()
    assert op.planned_wait_hours == D("4")
