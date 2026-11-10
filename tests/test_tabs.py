"""Record pages are tabbed; every tab renders and shows its own section."""

import re

import pytest

from trammel.billing import services as billing
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes


def tab_keys(html: str) -> list[str]:
    nav = html[html.index('<nav class="tabs"') :]
    nav = nav[: nav.index("</nav>")]
    return re.findall(r'href="\?tab=([a-z-]+)"', nav)


def walk(client, url):
    page = client.get(url).content.decode()
    keys = tab_keys(page)
    for key in keys:
        response = client.get(f"{url}?tab={key}")
        assert response.status_code == 200, (url, key)
        assert f'href="?tab={key}" aria-current="page"' in response.content.decode(), key
    return keys


@pytest.mark.django_db
def test_customer_and_job_tabs(admin_client, customer):
    job = jobs.create_job(Job(title="Sign", customer=customer))
    assert walk(admin_client, f"/customers/{customer.pk}/") == [
        "overview",
        "jobs",
        "quotes",
        "invoices",
        "files",
    ]
    assert walk(admin_client, f"/jobs/{job.pk}/") == [
        "overview",
        "quotes",
        "work-orders",
        "invoices",
        "files",
        "history",
    ]
    jobs_tab = admin_client.get(f"/customers/{customer.pk}/?tab=jobs").content.decode()
    assert job.number in jobs_tab
    overview = admin_client.get(f"/customers/{customer.pk}/").content.decode()
    assert "Contacts" in overview and job.number not in overview.split('<div class="tab-body">')[1]
    # An unknown tab falls back to the first.
    assert (
        'href="?tab=overview" aria-current="page"'
        in admin_client.get(f"/jobs/{job.pk}/?tab=nonsense").content.decode()
    )


@pytest.mark.django_db
def test_quote_and_invoice_tabs(admin_client, customer, admin):
    job = jobs.create_job(Job(title="Sign", customer=customer))
    rev = quotes.create_quote(job)
    assert walk(admin_client, f"/quotes/r/{rev.pk}/") == ["lines", "details", "preview", "files"]
    inv = billing.create(customer, kind="direct_sale")
    assert walk(admin_client, f"/invoices/{inv.pk}/") == ["lines", "details", "preview", "files"]


@pytest.mark.django_db
def test_settings_menu_is_grouped(admin_client):
    page = admin_client.get("/settings/business/").content.decode()
    sidebar = page[
        page.index('<nav class="sidebar"') : page.index(
            "</nav>", page.index('<nav class="sidebar"')
        )
    ]
    headings = re.findall(r"<h2>([^<]+)</h2>", sidebar)
    assert headings == ["Business", "Pricing and tax", "Documents", "People and access", "System"]
    assert sidebar.count('aria-current="page"') == 1


@pytest.mark.django_db
def test_main_menu_follows_the_workflow(admin_client):
    page = admin_client.get("/").content.decode()
    nav = page[page.index('<nav class="mainnav"') : page.index("</nav>")]
    labels = re.findall(r'">([A-Z][a-z ]+)(?: <span|</a>)', nav)
    assert labels == [
        "Customers",
        "Jobs",
        "Quotes",
        "Work orders",
        "Time",
        "Stock",
        "Products",
        "Invoices",
        "Calendar",
        "Reports",
        "Shop floor",
    ]
    calendar = admin_client.get("/jobs/calendar/").content.decode()
    assert calendar.count('aria-current="page"') == 1
