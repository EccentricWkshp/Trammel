"""The calendar by month, week or day, with a task list of what it shows."""

import datetime as dt

import pytest

from trammel.jobs import services
from trammel.jobs.models import Job

# Tuesday, November 10, 2026. The business week starts on Sunday by default.
DAY = dt.date(2026, 11, 10)


@pytest.fixture
def jobs(setup_done, customer):
    due = services.create_job(Job(title="Walnut sign", customer=customer, due_date=DAY))
    early = services.create_job(
        Job(title="Oak shelf", customer=customer, promised_date=DAY - dt.timedelta(days=1))
    )
    weekend = services.create_job(
        Job(title="Cedar box", customer=customer, target_completion=dt.date(2026, 11, 14))
    )
    delivered = services.create_job(Job(title="Maple tray", customer=customer, delivery_actual=DAY))
    return due, early, weekend, delivered


def _get(client, query):
    response = client.get(f"/jobs/calendar/?{query}")
    assert response.status_code == 200
    return response


@pytest.mark.django_db
def test_month_is_the_default_view(admin_client, jobs):
    response = _get(admin_client, f"date={DAY.isoformat()}")
    assert response.context["view"] == "month"
    assert "November 2026" in response.content.decode()
    tasks = [e.text for e, _ in response.context["tasks"]]
    assert len(tasks) == 4


@pytest.mark.django_db
def test_week_view_seven_days_or_the_work_week(admin_client, jobs):
    week = _get(admin_client, f"view=week&date={DAY.isoformat()}").context
    assert [d for d, _ in week["days"]][0] == dt.date(2026, 11, 8)  # Sunday
    assert len(week["days"]) == 7
    assert any(e.text.endswith("Cedar box") for e, _ in week["tasks"])  # Saturday

    work = _get(admin_client, f"view=week&week=work&date={DAY.isoformat()}").context
    days = [d for d, _ in work["days"]]
    assert days == [dt.date(2026, 11, 9) + dt.timedelta(days=i) for i in range(5)]  # Mon-Fri
    assert not any(e.text.endswith("Cedar box") for e, _ in work["tasks"])
    # The work week is remembered for next time.
    again = _get(admin_client, f"view=week&date={DAY.isoformat()}").context
    assert len(again["days"]) == 5
    nxt = again["next"]
    assert "date=2026-11-17" in nxt and "week=work" in nxt


@pytest.mark.django_db
def test_day_view_lists_that_day(admin_client, jobs):
    response = _get(admin_client, f"view=day&date={DAY.isoformat()}")
    texts = [e.text for e, _ in response.context["tasks"]]
    assert len(texts) == 2 and all(("Walnut sign" in t or "Maple tray" in t) for t in texts)
    page = response.content.decode()
    assert "Tuesday" in page and "date=2026-11-09" in page  # previous day


@pytest.mark.django_db
def test_task_list_dates_open_the_day(admin_client, jobs):
    page = _get(admin_client, f"date={DAY.isoformat()}").content.decode()
    task_list = page.split('id="tasks-heading"')[1]
    assert 'href="?view=day&amp;date=2026-11-14"' in task_list  # the Cedar box target


@pytest.mark.django_db
def test_task_list_is_soonest_first_with_done_items_marked(admin_client, jobs):
    due, early, weekend, delivered = jobs
    tasks = _get(admin_client, f"view=week&week=full&date={DAY.isoformat()}").context["tasks"]
    order = [(e.day, e.label, e.done) for e, _ in tasks]
    assert order == [
        (DAY - dt.timedelta(days=1), "Promised", False),
        (DAY, "Due", False),
        (DAY, "Delivered", True),  # what is done comes after what is still to do
        (dt.date(2026, 11, 14), "Target", False),
    ]
    page = _get(admin_client, f"view=week&date={DAY.isoformat()}").content.decode()
    assert "done" in page and "On the calendar" not in page and "This week" in page


@pytest.mark.django_db
def test_week_style_buttons_link_to_their_own_style(admin_client, jobs):
    styles = _get(admin_client, f"view=week&week=full&date={DAY.isoformat()}").context[
        "week_styles"
    ]
    urls = {s["key"]: s["url"] for s in styles}
    assert "week=work" in urls["work"] and "week=full" in urls["full"]
    days = _get(admin_client, urls["work"].lstrip("?")).context["days"]
    assert len(days) == 5


@pytest.mark.django_db
def test_default_view_is_kept_for_the_person(admin_client, admin, jobs):
    page = _get(admin_client, "").content.decode()
    assert "Your default view" in page  # month, until they choose otherwise

    response = admin_client.post(
        "/jobs/calendar/default/", {"view": "week", "week": "work", "date": DAY.isoformat()}
    )
    assert response.status_code == 302
    assert "view=week" in response["Location"] and "date=2026-11-10" in response["Location"]
    admin.refresh_from_db()
    assert admin.preferences["calendar"] == {"view": "week", "week": "work"}

    admin_client.logout()
    admin_client.force_login(admin)  # a fresh session still opens on the default
    context = _get(admin_client, f"date={DAY.isoformat()}").context
    assert context["view"] == "week" and len(context["days"]) == 5 and context["is_default"]
    assert not _get(admin_client, "view=month").context["is_default"]

    # Nonsense is ignored.
    admin_client.post("/jobs/calendar/default/", {"view": "year"})
    admin.refresh_from_db()
    assert admin.preferences["calendar"]["view"] == "week"


@pytest.mark.django_db
def test_late_only_once_the_day_has_passed(admin_client, jobs, monkeypatch):
    from django.utils import timezone

    monkeypatch.setattr(timezone, "localdate", lambda *a, **k: DAY)
    tasks = _get(admin_client, f"view=week&week=full&date={DAY.isoformat()}").context["tasks"]
    late = {e.label: e.late for e, _ in tasks}
    # Promised yesterday is late; due today and target Saturday are not.
    assert late == {"Promised": True, "Due": False, "Delivered": False, "Target": False}
