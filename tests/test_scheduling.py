"""The shop schedule (0.8.0): open work laid onto shop hours, the crew and the
machines, in queue order; shown as a timeline, on the work order, and in the API."""

import datetime as dt
from decimal import Decimal

import pytest

from trammel.core import workhours
from trammel.core.models import BusinessProfile
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job, Priority
from trammel.shop.models import Machine
from trammel.workorders import scheduling
from trammel.workorders import services as wos

D = Decimal
MONDAY = dt.datetime(2026, 11, 16, 8, 0)  # shop opens 8:00, Monday to Friday, till 4:00


@pytest.fixture
def shop(setup_done):
    profile = BusinessProfile.load()
    profile.timezone = "America/Los_Angeles"
    profile.shop_hours = [["08:00", "16:00"]] * 5 + [None, None]
    profile.shop_crew = [1] * 7
    profile.save()
    return profile


def _now():
    return workhours.Hours.load().aware(MONDAY)


def _wall(moment):
    return workhours.Hours.load().local(moment)


def _work_order(admin, title, *steps, priority=Priority.NORMAL, promised=None):
    """A work order on the shop's own job with operations (labor, machine
    minutes, machine, wait hours)."""
    job = jobs.create_job(Job(title=title, customer=Customer.internal(), priority=priority))
    if promised:
        job.promised_date = promised
        job.save()
    jobs.transition(job, "approved", actor=admin)
    wo = wos.create(job, title=title)
    for n, (labor, machine_minutes, machine, wait) in enumerate(steps, start=1):
        op = wos.add_operation(wo, name=f"{title} step {n}", machine=machine)
        wos.update_operation(
            op,
            name=op.name,
            machine=machine,
            planned_labor_minutes=D(labor) if labor else None,
            planned_machine_minutes=D(machine_minutes) if machine_minutes else None,
            planned_wait_hours=D(wait) if wait else None,
        )
    return wo


def _plan():
    return scheduling.build(now=_now())


def _span(plan, wo):
    found = plan.for_work_order(wo)
    return _wall(found.start), _wall(found.finish)


@pytest.mark.django_db
def test_rush_work_goes_first_then_the_earliest_promise(shop, admin):
    later = _work_order(admin, "Later", (60, 0, None, 0), promised=dt.date(2026, 12, 20))
    sooner = _work_order(admin, "Sooner", (60, 0, None, 0), promised=dt.date(2026, 12, 1))
    rush = _work_order(admin, "Rush", (60, 0, None, 0), priority=Priority.RUSH)
    plan = _plan()
    assert [p.wo for p in plan.work_orders] == [rush, sooner, later]
    # One person: one after another.
    assert [_span(plan, wo)[0].hour for wo in (rush, sooner, later)] == [8, 9, 10]


@pytest.mark.django_db
def test_the_crew_works_side_by_side_and_machines_one_job_at_a_time(shop, admin):
    laser = Machine.objects.create(name="Laser")
    printer = Machine.objects.create(name="Printer")
    a = _work_order(admin, "A", (0, 120, laser, 0))
    b = _work_order(admin, "B", (0, 120, laser, 0))
    c = _work_order(admin, "C", (0, 120, printer, 0))
    plan = _plan()
    assert _span(plan, a) == (MONDAY, MONDAY.replace(hour=10))
    assert _span(plan, b) == (MONDAY.replace(hour=10), MONDAY.replace(hour=12))  # the laser's free
    assert _span(plan, c) == (MONDAY, MONDAY.replace(hour=10))  # another machine, alongside
    # Two people: hands-on work side by side.
    shop.shop_crew = [2] * 7
    shop.save()
    d = _work_order(admin, "D", (60, 0, None, 0))
    e = _work_order(admin, "E", (60, 0, None, 0))
    plan = _plan()
    assert _span(plan, d)[0] == _span(plan, e)[0] == MONDAY


@pytest.mark.django_db
def test_operations_run_in_order_with_waits_and_across_days(shop, admin):
    glued = _work_order(admin, "Glue-up", (60, 0, None, 2), (30, 0, None, 0))
    plan = _plan().for_work_order(glued)
    first, second = plan.ops
    assert _wall(first.finish) == MONDAY.replace(hour=9)
    assert _wall(second.start) == MONDAY.replace(hour=11)  # after the two-hour clamp
    long = _work_order(admin, "Long", (600, 0, None, 0))
    start, finish = _span(_plan(), long)
    # Ten hours around the glue-up ahead of it in the queue (8–9 and 11–11:30
    # Monday): 6½ hours Monday, the rest Tuesday morning.
    assert start == MONDAY.replace(hour=9)
    assert finish == dt.datetime(2026, 11, 17, 11, 30)


@pytest.mark.django_db
def test_hands_on_time_on_a_machine_holds_the_machine_too(shop, admin):
    shop.shop_crew = [2] * 7
    shop.save()
    cnc = Machine.objects.create(name="CNC")
    setup = _work_order(admin, "Setup", (60, 60, cnc, 0))
    other = _work_order(admin, "Other", (0, 30, cnc, 0))
    plan = _plan()
    ops = plan.for_work_order(setup).ops[0]
    assert [(b.kind, _wall(b.start).hour, _wall(b.end).hour) for b in ops.bookings] == [
        ("labor", 8, 9),
        ("machine", 9, 10),
    ]
    assert _span(plan, other)[0] == MONDAY.replace(hour=10)


@pytest.mark.django_db
def test_only_whats_left_is_planned(shop, admin):
    from trammel.timetracking.models import TimeEntry

    wo = _work_order(admin, "Half done", (60, 0, None, 0))
    op = wo.operations.get()
    TimeEntry.objects.create(
        work_order=wo,
        operation=op,
        kind="labor",
        user=admin,
        started_at=_now() - dt.timedelta(days=3),
        ended_at=_now() - dt.timedelta(days=3) + dt.timedelta(minutes=30),
        minutes=D("30"),
    )
    assert _span(_plan(), wo) == (MONDAY, MONDAY.replace(minute=30))
    wos.set_operation(op, "done", user=admin)
    assert _plan().for_work_order(wo).finish is None  # nothing left


@pytest.mark.django_db
def test_late_work_and_waiting_work_orders(shop, admin):
    tight = _work_order(admin, "Tight", (900, 0, None, 0), promised=dt.date(2026, 11, 16))
    plan = _plan()
    assert plan.for_work_order(tight).late == ("the job's promised date", dt.date(2026, 11, 16))
    from trammel.workorders.models import Status

    wos.transition(tight, Status.IN_PROGRESS, actor=admin)
    wos.transition(
        tight, Status.WAITING, actor=admin, reason="Lumber is late", waiting_on="materials"
    )
    plan = _plan()
    assert plan.for_work_order(tight) is None and plan.waiting == [tight]


@pytest.mark.django_db
def test_the_schedule_page_the_work_order_and_the_api(shop, admin, admin_client):
    laser = Machine.objects.create(name="Laser")
    wo = _work_order(admin, "Sign", (30, 90, laser, 0), promised=dt.date(2030, 1, 1))
    page = admin_client.get("/work-orders/schedule/").content.decode()
    assert "Hands-on work" in page and "Laser" in page and wo.number in page
    assert page.count('class="bar ') >= 2 and "<meter" in page
    # Every run of bars is labeled with the short number, and the key names it.
    from trammel.workorders.schedule_views import short_number

    assert short_number("WO-2026-0007") == "7" and short_number("WO-2026-0000") == "0"
    short = short_number(wo.number)
    assert f'text-anchor="middle">{short}</text>' in page
    assert "schedule-key" in page and "Sign" in page and "schedule.js" in page
    detail = admin_client.get(wo.get_absolute_url()).content.decode()
    assert "Scheduled" in detail and "Number 1 in the queue" in detail
    listing = admin_client.get("/work-orders/").content.decode()
    assert "Scheduled finish" in listing
    body = admin_client.get(f"/api/v1/work-orders/{wo.pk}").json()
    assert body["queue_position"] == 1 and body["scheduled_finish"] == body["estimated_finish"]
    assert body["scheduled_start"] is not None


@pytest.mark.django_db
def test_the_crew_is_set_with_the_shop_hours(shop, admin_client):
    data = {"tab": "hours"}
    for d in range(7):
        works = d < 5
        data |= {
            f"day{d}_works": "on" if works else "",
            f"day{d}_starts": "08:00" if works else "",
            f"day{d}_ends": "16:00" if works else "",
            f"day{d}_crew": "3" if d == 0 else "1",
        }
    admin_client.post("/settings/business/?tab=hours", data)
    shop.refresh_from_db()
    assert shop.shop_crew[0] == 3 and shop.shop_crew[1] == 1
    hours = workhours.Hours.load()
    assert hours.crew_on(dt.date(2026, 11, 16)) == 3 and hours.crew_on(dt.date(2026, 11, 21)) == 0
