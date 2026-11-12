"""Machines that run unattended on the schedule (0.8.3): started in shop hours,
then straight through past closing, overnight or over a weekend."""

import datetime as dt

import pytest

from trammel.shop.models import Machine

from .test_scheduling import MONDAY, _plan, _span, _wall, _work_order, shop  # noqa: F401

TUESDAY = MONDAY + dt.timedelta(days=1)


@pytest.fixture
def printer(db):
    return Machine.objects.create(name="Printer", kind="printer_3d", runs_unattended=True)


def _bookings(plan, wo):
    return [
        (b.kind, b.unattended, _wall(b.start), _wall(b.end))
        for op in plan.for_work_order(wo).ops
        for b in op.bookings
    ]


@pytest.mark.django_db
def test_an_unattended_run_goes_on_past_closing(shop, admin, printer):  # noqa: F811
    first = _work_order(admin, "Long print", (30, 600, printer, 0))
    second = _work_order(admin, "Next print", (0, 120, printer, 0))
    plan = _plan()
    assert _bookings(plan, first) == [
        ("labor", False, MONDAY, MONDAY.replace(minute=30)),
        ("machine", True, MONDAY.replace(minute=30), MONDAY.replace(hour=18, minute=30)),
    ]
    # The printer's busy till 6:30 PM; the next print is started in the morning.
    assert _span(plan, second) == (TUESDAY, TUESDAY.replace(hour=10))


@pytest.mark.django_db
def test_the_crew_is_free_while_it_runs_and_it_can_start_at_closing(shop, admin, printer):  # noqa: F811
    overnight = _work_order(admin, "Overnight", (420, 1200, printer, 0))
    other = _work_order(admin, "Bench work", (60, 0, None, 0))
    plan = _plan()
    run = _bookings(plan, overnight)[-1]
    assert run == ("machine", True, MONDAY.replace(hour=15), TUESDAY.replace(hour=11))
    assert _span(plan, other)[0] == MONDAY.replace(hour=15)  # hands-on time, while it prints
    # A day of hands-on work, then the print started as the shop closes.
    late = _work_order(admin, "Late start", (0, 60, printer, 0))
    plan = _plan()
    assert _span(plan, late) == (TUESDAY.replace(hour=11), TUESDAY.replace(hour=12))
    closing = Machine.objects.create(name="Resin", runs_unattended=True)
    at_close = _work_order(admin, "At close", (0, 0, None, 0))
    at_close.operations.all().delete()
    from trammel.workorders import services as wos

    op = wos.add_operation(at_close, name="Cure", machine=closing)
    wos.update_operation(
        op, name="Cure", machine=closing, planned_labor_minutes=480, planned_machine_minutes=60
    )
    plan = _plan()
    # Its hands-on 8 hours wait for the crew, then the cure starts as they finish.
    labor, cure = _bookings(plan, at_close)[-2:]
    assert (
        cure[0] == "machine" and cure[2] == labor[3] and cure[3] - cure[2] == dt.timedelta(hours=1)
    )


@pytest.mark.django_db
def test_the_schedule_draws_a_run_on_each_day_it_covers(
    shop,  # noqa: F811
    admin,
    admin_client,
    printer,
    monkeypatch,
):
    import types

    from django.utils import timezone

    from trammel.core import workhours
    from trammel.workorders import scheduling

    # The page plans from now: make now the Monday morning these tests use.
    monday = workhours.Hours.load().aware(MONDAY)
    clock = types.SimpleNamespace(
        now=lambda: monday, localtime=timezone.localtime, is_aware=timezone.is_aware
    )
    monkeypatch.setattr(scheduling, "timezone", clock)
    _work_order(admin, "Overnight", (420, 1200, printer, 0))
    page = admin_client.get("/work-orders/schedule/?week=2026-11-15").content.decode()
    assert page.count(' unattended"') >= 2  # Monday afternoon and Tuesday morning
    assert "runs unattended" in page
    # The printer's load is out of the whole clock, not shop hours.
    assert "Printer" in page
    form = admin_client.get(f"/shop/machines/{printer.pk}/").content.decode()
    assert 'name="runs_unattended"' in form
