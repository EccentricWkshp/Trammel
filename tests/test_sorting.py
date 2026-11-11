"""Sorting list pages by their columns (core/sorting.py): column headings that
sort and reverse, the same choices as a menu, filters kept, blanks last."""

import re

import pytest
from django.test import RequestFactory
from django.urls import reverse

from trammel.core import modules, sorting
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job


def _numbers(page: str) -> list[str]:
    return re.findall(r"<strong>(J-\d{4}-\d{4})</strong>", page)


@pytest.fixture
def three_jobs(setup_done):
    import datetime as dt

    zed = Customer.objects.create(name="Zed Woodworks")
    amy = Customer.objects.create(name="Amy Archer")
    made = []
    for title, customer, due in (
        ("Bench", zed, dt.date(2026, 12, 1)),
        ("Shelf", amy, None),
        ("Table", amy, dt.date(2026, 11, 20)),
    ):
        made.append(jobs.create_job(Job(title=title, customer=customer, due_date=due)))
    return made


@pytest.mark.django_db
def test_columns_sort_and_reverse(admin_client, three_jobs):
    bench, shelf, table = (j.number for j in three_jobs)
    # By due date by default, the job with none last.
    page = admin_client.get("/jobs/").content.decode()
    assert _numbers(page) == [table, bench, shelf]
    assert 'aria-sort="ascending"' in page
    page = admin_client.get("/jobs/?sort=customer").content.decode()
    assert _numbers(page)[2] == bench  # Zed last
    page = admin_client.get("/jobs/?sort=-number").content.decode()
    assert _numbers(page) == sorted([bench, shelf, table], reverse=True)
    assert 'aria-sort="descending"' in page
    # Blank due dates stay last the other way round too.
    page = admin_client.get("/jobs/?sort=-due").content.decode()
    assert _numbers(page) == [bench, table, shelf]
    # Unknown sorts fall back to the default.
    assert _numbers(admin_client.get("/jobs/?sort=nonsense").content.decode()) == [
        table,
        bench,
        shelf,
    ]


@pytest.mark.django_db
def test_headings_keep_filters_and_toggle(admin_client, three_jobs):
    page = admin_client.get("/jobs/?q=Bench&status=all&sort=number&page=1").content.decode()
    heading = re.search(
        r'<th scope="col" aria-sort="ascending"><a class="sort-link" href="([^"]+)"', page
    )
    url = heading.group(1).replace("&amp;", "&")
    # The sorted column's link reverses it, keeps the filters and starts at page one.
    assert "sort=-number" in url and "q=Bench" in url and "status=all" in url
    assert "page=" not in url
    # The same sorts as a menu in the filter form, reversed shown when chosen.
    page = admin_client.get("/jobs/?sort=-customer").content.decode()
    assert '<option value="-customer" selected>Customer, reversed</option>' in page
    assert '<option value="priority">Priority, then due date</option>' in page
    # The CSV export follows the sort.
    csv = admin_client.get("/jobs/csv/?sort=-number&status=all").content.decode()
    numbers = re.findall(r"J-\d{4}-\d{4}", csv)
    assert numbers == sorted(numbers, reverse=True)


def test_statuses_sort_in_lifecycle_order():
    from trammel.jobs.models import Status

    expression = sorting.in_order("status", Status.values)
    assert len(expression.cases) == len(Status.values)
    request = RequestFactory().get("/", {"sort": "status"})
    sorts = sorting.Sorts({"status": sorting.By("Status", expression)}, default="status")
    assert sorts.current(request) == ("status", False)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("name", "extra", "sorts"),
    [
        ("jobs:list", {}, "trammel.jobs.views"),
        ("quotes:list", {}, "trammel.quotes.views"),
        ("quotes:list", {"kind": "change_order"}, "trammel.quotes.views"),
        ("workorders:list", {}, "trammel.workorders.views"),
        ("billing:list", {}, "trammel.billing.views"),
        ("customers:list", {}, "trammel.customers.views"),
        ("catalog:list", {}, "trammel.catalog.views"),
        ("purchasing:list", {}, "trammel.purchasing.views"),
        ("time:list", {}, "trammel.timetracking.views"),
        ("stock:list", {}, "trammel.stock.views"),
        ("shop:vendors", {}, "trammel.shop.views.VENDOR_SORTS"),
        ("shop:materials", {}, "trammel.shop.views.MATERIAL_SORTS"),
        ("imports:list", {}, "trammel.imports.views"),
    ],
)
def test_every_sort_on_every_list_works(admin_client, setup_done, name, extra, sorts):
    """Each sort, both ways, on each list page: no field a sort names is missing."""
    import importlib

    for module in ("purchasing", "imports", "stock"):
        modules.set_enabled(module, True)
    if name == "shop:materials":
        # With stock on, the stock list is the materials page.
        modules.set_enabled("purchasing", False)
        modules.set_enabled("stock", False)
    path, attr = sorts.rsplit(".", 1) if sorts.endswith("_SORTS") else (sorts, "SORTS")
    found = getattr(importlib.import_module(path), attr)
    url = reverse(name)
    for key in found.choices:
        for value in (key, f"-{key}"):
            response = admin_client.get(url, {**extra, "sort": value})
            assert response.status_code == 200, (url, value)
            # Evaluated, not just built: the page lists rows from the query.
            response.content.decode()


@pytest.mark.django_db
def test_no_sorting_by_what_someone_cannot_see(client, setup_done):
    """Ordered by cost, a list would give away costs to someone who can't see them."""
    from tests.conftest import make_user

    modules.set_enabled("stock", True)
    client.force_login(make_user("viewer@example.com", "shop.view", "time.view"))
    page = client.get("/stock/").content.decode()
    menu = page.split('name="sort"')[1].split("</select>")[0]
    assert 'value="name"' in menu
    assert not any(f'value="{k}"' in menu for k in ("cost", "value", "on_hand", "restock"))
    assert 'aria-sort="ascending"><a class="sort-link" href="?sort=-name"' in client.get(
        "/stock/?sort=-cost"
    ).content.decode().replace("&amp;", "&")
    menu = client.get("/time/").content.decode().split('name="sort"')[1].split("</select>")[0]
    assert 'value="minutes"' in menu and 'value="cost"' not in menu
