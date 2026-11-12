"""The schedule sheet and the shop-floor Schedule (0.8.3): each day's work in
the order it's done, printed or as a PDF to take into the shop, and on the
floor app; the traveler's when to its what."""

import datetime as dt
import types

import pytest
from django.utils import timezone

from trammel.core import templating, workhours
from trammel.shop.models import Machine
from trammel.workorders import scheduling, sheet

from .test_scheduling import MONDAY, _work_order, shop  # noqa: F401


@pytest.fixture
def monday(monkeypatch):
    """Plan from Monday 8:00, the morning these tests use."""
    now = workhours.Hours.load().aware(MONDAY)
    monkeypatch.setattr(
        scheduling,
        "timezone",
        types.SimpleNamespace(
            now=lambda: now, localtime=timezone.localtime, is_aware=timezone.is_aware
        ),
    )
    return now


@pytest.fixture
def work(shop, admin, monday):  # noqa: F811
    laser = Machine.objects.create(name="Laser")
    printer = Machine.objects.create(name="Printer", runs_unattended=True)
    a = _work_order(admin, "Sign", (30, 60, laser, 0))
    b = _work_order(admin, "Bracket", (15, 1200, printer, 0))
    return laser, printer, a, b


@pytest.mark.django_db
def test_the_days_work_in_order(work):
    laser, printer, a, b = work
    days = sheet.days(scheduling.build(), MONDAY.date(), 2)
    monday, tuesday = days
    names = [(i["wo"].title, i["kind"]) for i in monday["items"]]
    assert names == [
        ("Sign", "labor"),
        ("Sign", "machine"),
        ("Bracket", "labor"),
        ("Bracket", "machine"),
    ]
    run = monday["items"][-1]
    assert run["unattended"] and run["overnight"]  # prints into Tuesday
    # Just one machine, or just the hands-on work.
    only = sheet.days(scheduling.build(), MONDAY.date(), 1, str(laser.pk))[0]["items"]
    assert {i["machine"] for i in only} == {laser}
    hands = sheet.days(scheduling.build(), MONDAY.date(), 1, "labor")[0]["items"]
    assert {i["kind"] for i in hands} == {"labor"}
    # A run from the day before opens the next day's sheet.
    tuesday = sheet.days(scheduling.build(), MONDAY.date().replace(day=17), 1)[0]
    assert tuesday["items"][0]["continued"]


@pytest.mark.django_db
def test_the_printed_sheet(work, admin_client):
    laser, printer, a, b = work
    url = "/work-orders/schedule/sheet/?from=2026-11-16&days=2"
    page = admin_client.get(url + "&print=1").content.decode()
    assert "Shop schedule" in page and a.number in page and b.number in page
    assert page.count('class="box"') == 4 and "Runs unattended" in page
    assert "<svg" in page and "print()" in page  # the QR code, and the print dialog
    assert "The queue" in page
    # A due date standing in for a promise reads like every other date.
    a.job.due_date = dt.date(2026, 12, 4)
    a.job.save()
    for url_ in (url, "/work-orders/schedule/"):
        assert "12/04/2026" in admin_client.get(url_).content.decode()
    one = admin_client.get(url + f"&for={printer.pk}&per_page=1").content.decode()
    # The printer's sheet: setting it up, and the run.
    assert one.count('class="box"') == 2 and "Printer" in one and "new-page" in one
    # Linked from the Schedule page.
    page = admin_client.get("/work-orders/schedule/").content.decode()
    assert "/work-orders/schedule/sheet/" in page and "Download PDF" in page
    if templating.pdf_available():
        pdf = admin_client.get(url + "&format=pdf")
        assert pdf["Content-Type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
        assert "schedule-2026-11-16.pdf" in pdf["Content-Disposition"]


@pytest.mark.django_db
def test_the_shop_floor_schedule(work, admin_client):
    laser, printer, a, b = work
    page = admin_client.get("/floor/schedule/?day=2026-11-16").content.decode()
    assert f"/floor/wo/{a.pk}/" in page and f"/floor/wo/{b.pk}/" in page
    assert page.count("floor-card") >= 4 and "Print this day" in page
    assert "runs unattended" in page
    home = admin_client.get("/floor/").content.decode()
    assert "/floor/schedule/" in home  # in the floor app's menu
    narrowed = admin_client.get(f"/floor/schedule/?day=2026-11-16&for={laser.pk}").content
    assert b.number not in narrowed.decode()
