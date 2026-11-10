"""The home screen: sections from each module, arranged by each user."""

import datetime as dt
import re

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tests.conftest import make_user
from trammel.core import dashboard, modules
from trammel.jobs import services as jobs
from trammel.jobs.models import Job, Priority


def _titles(page: str) -> list[str]:
    return re.findall(r'<h2 id="home-[^"]+">([^<]+?)(?: <span|</h2>)', page)


@pytest.mark.django_db
def test_admin_sees_every_section_in_default_order(admin_client):
    page = admin_client.get("/").content.decode()
    assert _titles(page) == [
        "Priority jobs",
        "Jobs on hold",
        "Quotes awaiting approval",
        "Open work orders",
        "Low stock",
        "Deliveries expected",
        "Overdue invoices",
        "Unissued invoices",
        "Shortcuts",
    ]
    assert "No rush, high-priority or late jobs." in page


@pytest.mark.django_db
def test_priority_jobs_lists_rush_high_due_soon_and_late(admin_client, customer):
    today = dt.date.today()
    late = jobs.create_job(
        Job(title="Late sign", customer=customer, due_date=today - dt.timedelta(days=2))
    )
    rush = jobs.create_job(Job(title="Rush plaque", customer=customer, priority=Priority.RUSH))
    soon = jobs.create_job(
        Job(title="Due Friday", customer=customer, due_date=today + dt.timedelta(days=1))
    )
    calm = jobs.create_job(
        Job(title="Someday shelf", customer=customer, due_date=today + dt.timedelta(days=30))
    )
    page = admin_client.get("/").content.decode()
    section = page[page.index("Priority jobs") : page.index("Jobs on hold")]
    assert calm.number not in section
    # Late work first, then by priority, then by due date.
    assert section.index(late.number) < section.index(rush.number) < section.index(soon.number)


@pytest.mark.django_db
def test_sections_follow_permissions_and_enabled_modules(client, admin, monkeypatch):
    user = make_user("viewer@example.com", "jobs.view")
    client.force_login(user)
    assert _titles(client.get("/").content.decode()) == [
        "Priority jobs",
        "Jobs on hold",
        "Shortcuts",
    ]
    # Billing cannot really be switched off while modules that need it are on.
    on = modules.enabled_keys()
    monkeypatch.setattr(modules, "enabled_keys", lambda: on - {"billing"})
    client.force_login(admin)
    titles = _titles(client.get("/").content.decode())
    assert "Overdue invoices" not in titles and "Open work orders" in titles


@pytest.mark.django_db
def test_user_arranges_their_own_home(admin_client, admin):
    page = admin_client.get("/home/customize/").content.decode()
    assert 'name="order-jobs.priority" value="1"' in page
    keys = [
        d.key for d, _ in dashboard.arranged(type("R", (), {"user": admin, "actor": _actor(admin)}))
    ]
    data = {f"order-{k}": str(n) for n, k in enumerate(keys, start=1)}
    data |= {f"on-{k}": "on" for k in keys if k != "jobs.on_hold"}
    data["order-billing.overdue"] = "0"  # put first
    assert admin_client.post("/home/customize/", data).status_code == 302
    titles = _titles(admin_client.get("/").content.decode())
    assert titles[0] == "Overdue invoices" and "Jobs on hold" not in titles
    admin.refresh_from_db()
    assert {"key": "jobs.on_hold", "on": False} in admin.home_sections
    admin_client.post("/home/customize/", {"reset": "1"})
    admin.refresh_from_db()
    assert admin.home_sections == []
    assert _titles(admin_client.get("/").content.decode())[0] == "Priority jobs"


@pytest.mark.django_db
def test_a_new_section_appears_in_its_default_place(admin):
    admin.home_sections = [
        {"key": "core.shortcuts", "on": True},
        {"key": "jobs.priority", "on": False},
    ]
    request = type("R", (), {"user": admin, "actor": _actor(admin)})
    arranged = [(d.key, on) for d, on in dashboard.arranged(request)]
    assert arranged[0] == ("core.shortcuts", True)
    assert ("jobs.priority", False) in arranged
    # Sections the user never arranged are on and slot in by their default order.
    assert ("billing.overdue", True) in arranged


@pytest.mark.django_db
def test_home_screen_query_count_stays_small(admin_client, customer):
    for n in range(15):
        jobs.create_job(Job(title=f"Rush {n}", customer=customer, priority=Priority.RUSH))
    admin_client.get("/")  # warm caches (modules, settings)
    with CaptureQueriesContext(connection) as queries:
        page = admin_client.get("/").content.decode()
    assert "See all 15" in page
    assert len(queries) < 40, [q["sql"][:120] for q in queries]


def _actor(user):
    from trammel.core import access

    return access.as_actor(user)
