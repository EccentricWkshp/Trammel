"""Starting records from their own list pages: quotes, work orders, and catalog
products from a quote line."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from tests.conftest import make_user
from trammel.catalog.models import Product
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.models import Quote, QuoteLine
from trammel.workorders.models import WorkOrder


@pytest.mark.django_db
def test_new_quote_for_an_open_job(admin_client, customer):
    job = jobs.create_job(Job(title="Walnut sign", customer=customer))
    page = admin_client.get(f"/quotes/new/?job={job.pk}").content.decode()
    assert f'<option value="{job.pk}" selected>' in page
    response = admin_client.post(
        "/quotes/new/", {"mode": "existing", "job": job.pk, "title": "Sign, option B"}
    )
    quote = Quote.objects.get(job=job)
    assert response.status_code == 302 and quote.title == "Sign, option B"


@pytest.mark.django_db
def test_new_quote_with_a_new_job(admin_client, customer):
    response = admin_client.post(
        "/quotes/new/",
        {"mode": "new", "new-title": "Cutting boards", "new-customer": customer.pk},
    )
    assert response.status_code == 302
    job = Job.objects.get(title="Cutting boards")
    assert job.customer == customer and Quote.objects.filter(job=job).count() == 1


@pytest.mark.django_db
def test_new_job_is_not_kept_when_its_quote_fails(admin_client, monkeypatch):
    def refuse(job, **kwargs):
        raise ValidationError("No quote today.")

    monkeypatch.setattr(quotes, "create_quote", refuse)
    response = admin_client.post("/quotes/new/", {"mode": "new", "new-title": "Doomed"})
    assert "No quote today." in response.content.decode()
    assert not Job.objects.filter(title="Doomed").exists()


@pytest.mark.django_db
def test_new_job_option_needs_job_permission(client, setup_done, customer):
    user = make_user("q@example.com", "quotes.view", "quotes.create", "jobs.view")
    client.force_login(user)
    page = client.get("/quotes/new/").content.decode()
    assert "For an open job" in page and "With a new job" not in page
    client.post("/quotes/new/", {"mode": "new", "new-title": "Sneaky"})
    assert not Job.objects.filter(title="Sneaky").exists()


@pytest.mark.django_db
def test_new_work_order_offers_only_ready_jobs(admin_client, admin, customer):
    ready = jobs.create_job(Job(title="Jig", customer=Customer.internal()))
    jobs.transition(ready, "approved", actor=admin)
    waiting = jobs.create_job(Job(title="Not approved", customer=customer))
    page = admin_client.get("/work-orders/new/").content.decode()
    assert str(ready.pk) in page and str(waiting.pk) not in page
    page = admin_client.get(f"/work-orders/new/?job={ready.pk}").content.decode()
    assert f'action="/work-orders/new/{ready.pk}/"' in page and "Blank work order" in page
    response = admin_client.post(f"/work-orders/new/{ready.pk}/", {"revision": "", "title": ""})
    assert response.status_code == 302 and WorkOrder.objects.get(job=ready).title == "Jig"
    assert "New work order" in admin_client.get("/work-orders/").content.decode()


@pytest.fixture
def draft(customer):
    return quotes.create_quote(jobs.create_job(Job(title="Coasters", customer=customer)))


@pytest.mark.django_db
def test_quote_line_adds_a_new_catalog_product(admin_client, draft):
    url = f"/quotes/r/{draft.pk}/lines/new/product/"
    assert "Not in the catalog? Add a new product" in admin_client.get(url).content.decode()
    response = admin_client.post(
        url,
        {
            "product": "",
            "new_name": "Slate coaster",
            "new_sku": "SC-1",
            "new_kind": "resold",
            "new_price": "12.50",
            "new_cost": "4.10",
            "new_unit": "each",
            "quantity": "4",
        },
    )
    assert response.status_code == 302
    product = Product.objects.get(sku="SC-1")
    assert (product.name, product.kind, product.price) == (
        "Slate coaster",
        "resold",
        Decimal("12.50"),
    )
    line = QuoteLine.objects.get(revision=draft)
    assert line.product == product and line.unit_price == Decimal("12.50")


@pytest.mark.django_db
def test_new_product_mistakes_are_shown_and_nothing_is_saved(admin_client, draft):
    existing = Product.objects.create(name="Coaster", sku="C-1", price=Decimal("8"))
    url = f"/quotes/r/{draft.pk}/lines/new/product/"
    both = admin_client.post(url, {"product": existing.pk, "new_name": "Other", "quantity": "1"})
    assert "not both" in both.content.decode()
    neither = admin_client.post(url, {"product": "", "quantity": "1"})
    assert "Choose a product, or add a new one below." in neither.content.decode()
    no_price = admin_client.post(url, {"product": "", "new_name": "Thing", "quantity": "1"})
    assert "A new product needs a price." in no_price.content.decode()
    taken = admin_client.post(
        url, {"product": "", "new_name": "Dup", "new_sku": "C-1", "new_price": "1", "quantity": "1"}
    )
    assert taken.status_code == 200 and "form-errors" in taken.content.decode()
    assert Product.objects.count() == 1 and not QuoteLine.objects.filter(revision=draft).exists()


@pytest.mark.django_db
def test_new_product_needs_catalog_permission(client, draft):
    user = make_user("q2@example.com", "quotes.view", "quotes.edit", "jobs.view", "customers.view")
    client.force_login(user)
    url = f"/quotes/r/{draft.pk}/lines/new/product/"
    assert "Add a new product" not in client.get(url).content.decode()
    client.post(url, {"product": "", "new_name": "Sneaky", "new_price": "1", "quantity": "1"})
    assert not Product.objects.filter(name="Sneaky").exists()
