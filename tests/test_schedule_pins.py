"""Moves and pins on the shop schedule (0.8.1): work orders moved up and down
the queue, operations pinned to a day or to their machine, and machines in a
group standing in for each other."""

import datetime as dt

import pytest
from django.core.exceptions import ValidationError

from trammel.core.models import AuditEvent
from trammel.jobs.models import Priority
from trammel.shop.models import Machine
from trammel.workorders import services as wos
from trammel.workorders.models import Status

from .test_scheduling import MONDAY, _plan, _span, _wall, _work_order, shop  # noqa: F401

TUESDAY = MONDAY + dt.timedelta(days=1)


def _order(plan):
    return [p.wo.title for p in plan.work_orders]


@pytest.mark.django_db
def test_moving_work_orders_up_and_down_the_queue(shop, admin):  # noqa: F811
    _work_order(admin, "A", (60, 0, None, 0), priority=Priority.RUSH)
    b = _work_order(admin, "B", (60, 0, None, 0))
    c = _work_order(admin, "C", (60, 0, None, 0))
    assert _order(_plan()) == ["A", "B", "C"]
    wos.move_in_queue(c, "up")
    plan = _plan()
    assert _order(plan) == ["A", "C", "B"]
    assert plan.for_work_order(c).moved and not plan.for_work_order(b).moved
    assert _span(plan, c)[0] == MONDAY.replace(hour=9)  # people go to it first
    wos.move_in_queue(c, "up")
    assert _order(_plan()) == ["C", "A", "B"]
    with pytest.raises(ValidationError, match="already first"):
        wos.move_in_queue(c, "up")
    # New work still sorts in by priority around the moves.
    _work_order(admin, "D", (60, 0, None, 0), priority=Priority.RUSH)
    assert _order(_plan()) == ["C", "A", "D", "B"]
    with pytest.raises(ValidationError, match="already last"):
        wos.move_in_queue(b, "down")


@pytest.mark.django_db
def test_moves_back_down_and_when_the_other_work_order_leaves(shop, admin):  # noqa: F811
    a = _work_order(admin, "A", (60, 0, None, 0), priority=Priority.RUSH)
    _work_order(admin, "B", (60, 0, None, 0))
    c = _work_order(admin, "C", (60, 0, None, 0))
    wos.move_in_queue(a, "down")
    assert _order(_plan()) == ["B", "A", "C"]
    with pytest.raises(ValidationError, match="already last"):
        wos.move_in_queue(c, "down")
    wos.move_in_queue(a, "back")
    assert _order(_plan()) == ["A", "B", "C"]
    assert AuditEvent.objects.filter(action="workorder.queue").count() == 2
    # A move next to a work order that's left the queue no longer applies.
    wos.move_in_queue(c, "up")
    wos.move_in_queue(c, "up")
    assert _order(_plan()) == ["C", "A", "B"]
    wos.transition(a, Status.IN_PROGRESS, actor=admin)
    wos.transition(a, Status.WAITING, actor=admin, reason="Glue", waiting_on="materials")
    assert _order(_plan()) == ["B", "C"]
    with pytest.raises(ValidationError, match="isn't in the queue"):
        wos.move_in_queue(a, "up")


@pytest.mark.django_db
def test_an_operation_pinned_to_a_day_comes_first_that_day(shop, admin):  # noqa: F811
    first = _work_order(admin, "First", (120, 0, None, 0), priority=Priority.HIGH)
    later = _work_order(admin, "Later", (60, 0, None, 0), (60, 0, None, 0))
    step1, step2 = later.operations.order_by("position")
    wos.pin_operation(step2, pinned_date=TUESDAY.date())
    plan = _plan()
    pinned = plan.for_work_order(later).ops[1]
    assert pinned.pinned and not pinned.too_early
    assert (_wall(pinned.start), _wall(pinned.finish)) == (TUESDAY, TUESDAY.replace(hour=9))
    assert _span(plan, first) == (MONDAY, MONDAY.replace(hour=10))
    # Pinned to Monday: it takes Monday's first hour, ahead of the queue, but
    # the step before it can't be done by then.
    wos.pin_operation(step2, pinned_date=MONDAY.date())
    plan = _plan()
    pinned = plan.for_work_order(later).ops[1]
    assert _wall(pinned.start) == MONDAY and pinned.too_early
    assert plan.for_work_order(later).too_early == [pinned]
    assert _span(plan, first) == (MONDAY.replace(hour=9), MONDAY.replace(hour=11))
    # A day in the past is planned from now; a day off from the next opening.
    wos.pin_operation(step1, pinned_date=dt.date(2026, 11, 21))  # a Saturday
    wos.pin_operation(step2, pinned_date=None)
    plan = _plan()
    assert _wall(plan.for_work_order(later).ops[0].start) == dt.datetime(2026, 11, 23, 8, 0)
    assert AuditEvent.objects.filter(action="operation.pin").count() == 4


@pytest.mark.django_db
def test_machines_in_a_group_stand_in_for_each_other(shop, admin):  # noqa: F811
    p1 = Machine.objects.create(name="Printer 1", group="FDM printers")
    p2 = Machine.objects.create(name="Printer 2", group="fdm Printers ")
    Machine.objects.create(name="Printer 3", group="FDM printers", is_active=False)
    laser = Machine.objects.create(name="Laser")
    a = _work_order(admin, "A", (0, 120, p1, 0), (0, 60, laser, 0))
    b = _work_order(admin, "B", (0, 120, p1, 0))
    c = _work_order(admin, "C", (0, 120, p1, 0))
    plan = _plan()
    on = {wo.title: plan.for_work_order(wo).ops[0] for wo in (a, b, c)}
    assert on["A"].machine_id == p1.pk and not on["A"].other_machine
    assert on["B"].machine_id == p2.pk and on["B"].other_machine  # free at once
    assert _span(plan, b)[0] == MONDAY
    assert on["C"].machine_id == p1.pk and _span(plan, c)[0] == MONDAY.replace(hour=10)
    assert all(b.machine_id == p2.pk for b in on["B"].bookings)
    # Kept on its own machine, it waits for it.
    op = b.operations.get()
    wos.pin_operation(op, machine_pinned=True)
    plan = _plan()
    assert plan.for_work_order(b).ops[0].machine_id == p1.pk
    assert _span(plan, b)[0] == MONDAY.replace(hour=10)
    assert plan.for_work_order(c).ops[0].machine_id == p2.pk


@pytest.mark.django_db
def test_pins_moves_and_groups_on_the_pages_and_api(shop, admin, admin_client):  # noqa: F811
    p1 = Machine.objects.create(name="Printer 1", group="FDM printers")
    Machine.objects.create(name="Printer 2", group="FDM printers")
    a = _work_order(admin, "Alpha", (0, 120, p1, 0))
    b = _work_order(admin, "Bravo", (0, 120, p1, 0), (30, 0, None, 0))
    op, second = b.operations.order_by("position")

    # The operation form pins it.
    admin_client.post(
        f"/work-orders/{b.pk}/operations/{second.pk}/edit/",
        {
            "name": second.name,
            "planned_labor_minutes": "30",
            "pins": "1",
            "pinned_date": "2030-01-07",
            "machine_pinned": "on",
        },
    )
    second.refresh_from_db()
    assert second.pinned_date == dt.date(2030, 1, 7) and second.machine_pinned
    # Saved without the pin fields (another form), the pins stay.
    wos.update_operation(second, name="Pack", planned_labor_minutes=30)
    second.refresh_from_db()
    assert second.pinned_date == dt.date(2030, 1, 7)
    page = admin_client.get(b.get_absolute_url()).content.decode()
    assert "Pinned to" in page and "Scheduled on Printer 2" in page

    # The queue: moved from the Schedule page, and back.
    page = admin_client.get("/work-orders/schedule/").content.decode()
    assert f"Move {b.number} up" in page and "Back to its place" not in page
    response = admin_client.post(f"/work-orders/{b.pk}/queue/", {"direction": "up"})
    assert response.status_code == 302 and response["Location"] == "/work-orders/schedule/"
    page = admin_client.get("/work-orders/schedule/").content.decode()
    assert "Back to its place" in page and "instead of Printer 1" in page

    # The API.
    body = admin_client.get(f"/api/v1/work-orders/{b.pk}").json()
    assert body["queue_moved"] and body["queue_position"] == 1
    first = body["operations"][0]
    assert first["scheduled_machine"] == str(p1.pk)
    assert body["operations"][1]["pinned_date"] == "2030-01-07"
    body = admin_client.post(
        f"/api/v1/work-orders/{a.pk}/operations/{a.operations.get().pk}/pins",
        {"pinned_date": None, "machine_pinned": True},
        content_type="application/json",
    ).json()
    assert body["operations"][0]["machine_pinned"] is True
    body = admin_client.post(
        f"/api/v1/work-orders/{b.pk}/queue", {"direction": "back"}, content_type="application/json"
    ).json()
    assert body["queue_moved"] is False
    listed = admin_client.get("/api/v1/work-orders/").json()
    assert {w["queue_position"] for w in listed} == {1, 2}

    # Machine groups are named on the machine, with the groups already used offered.
    page = admin_client.get(f"/shop/machines/{p1.pk}/").content.decode()
    assert 'list="machine-groups"' in page
    assert '<option value="FDM printers">' in page


@pytest.mark.django_db
def test_a_pinned_operation_can_be_finished_outside_shop_hours(shop, admin, admin_client):  # noqa: F811
    big = _work_order(admin, "Big", (600, 0, None, 0))
    other = _work_order(admin, "Other", (60, 0, None, 0), priority=Priority.RUSH)
    op = big.operations.get()
    # Pinned to Monday without staying late: 8 hours Monday, 2 more Tuesday.
    wos.pin_operation(op, pinned_date=MONDAY.date())
    plan = _plan()
    assert _span(plan, big) == (MONDAY, TUESDAY.replace(hour=10))
    # Finished that day, staying late: till 6:00 PM, and nothing else is
    # planned after hours (the rush work waits for Tuesday).
    wos.pin_operation(op, pinned_date=MONDAY.date(), pinned_overtime=True)
    plan = _plan()
    assert _span(plan, big) == (MONDAY, MONDAY.replace(hour=18))
    hours = [
        (b.after_hours, _wall(b.start).hour, _wall(b.end).hour)
        for b in plan.for_work_order(big).ops[0].bookings
    ]
    assert hours == [(False, 8, 16), (True, 16, 18)]
    assert _span(plan, other)[0] == TUESDAY
    # Coming in early instead (Tuesday: it's 8:00 Monday now, so a start
    # earlier today is planned from now).
    wos.pin_operation(op, pinned_date=TUESDAY.date(), pinned_from=dt.time(6), pinned_overtime=True)
    assert _span(_plan(), big) == (TUESDAY.replace(hour=6), TUESDAY.replace(hour=16))
    wos.pin_operation(op, pinned_date=MONDAY.date(), pinned_from=dt.time(6), pinned_overtime=True)
    assert _span(_plan(), big) == (MONDAY, MONDAY.replace(hour=18))
    # A start time alone stays in shop hours.
    wos.pin_operation(op, pinned_date=TUESDAY.date(), pinned_from=dt.time(10))
    assert _span(_plan(), big)[0] == TUESDAY.replace(hour=10)
    # A day off: from the start time (else the usual opening), all of it outside shop hours.
    wos.pin_operation(
        op, pinned_date=dt.date(2026, 11, 21), pinned_from=dt.time(9), pinned_overtime=True
    )
    plan = _plan()
    saturday = dt.datetime(2026, 11, 21, 9, 0)
    assert _span(plan, big) == (saturday, saturday.replace(hour=19))
    assert all(b.after_hours for b in plan.for_work_order(big).ops[0].bookings)
    wos.pin_operation(op, pinned_date=dt.date(2026, 11, 21), pinned_overtime=True)
    assert _span(_plan(), big)[0] == saturday.replace(hour=8)
    # More than the day holds: on at the next opening.
    wos.update_operation(op, name=op.name, planned_labor_minutes=1200)
    wos.pin_operation(op, pinned_date=MONDAY.date(), pinned_overtime=True)
    assert _span(_plan(), big) == (MONDAY, TUESDAY.replace(hour=12))

    # The Schedule page widens Monday and shades the time outside shop hours.
    page = admin_client.get("/work-orders/schedule/?week=2026-11-16").content.decode()
    assert 'class="after-hours"' in page and "outside shop hours" in page
    # The operation form: the start time and staying late go with the day.
    admin_client.post(
        f"/work-orders/{big.pk}/operations/{op.pk}/edit/",
        {
            "name": op.name,
            "pins": "1",
            "pinned_date": "2026-11-18",
            "pinned_from": "07:30",
            "pinned_overtime": "on",
        },
    )
    op.refresh_from_db()
    assert (op.pinned_date, op.pinned_from, op.pinned_overtime) == (
        dt.date(2026, 11, 18),
        dt.time(7, 30),
        True,
    )
    page = admin_client.get(big.get_absolute_url()).content.decode()
    assert "from 7:30 AM, outside shop hours if need be" in page
    admin_client.post(
        f"/work-orders/{big.pk}/operations/{op.pk}/edit/",
        {"name": op.name, "pins": "1", "pinned_from": "07:30", "pinned_overtime": "on"},
    )
    op.refresh_from_db()
    assert (op.pinned_date, op.pinned_from, op.pinned_overtime) == (None, None, False)
