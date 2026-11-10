"""The product page: archiving and deleting products (FR-PRD-1)."""

from decimal import Decimal

import pytest

from tests.conftest import make_user
from trammel.catalog.models import Product
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS


@pytest.fixture
def product(setup_done):
    return Product.objects.create(name="Cutting board", sku="CB-1", price=Decimal("85"))


def _quote(product):
    job = jobs.create_job(Job(title="Boards", customer=Customer.internal()))
    form = LINE_FORMS["product"]({"product": str(product.pk), "quantity": "1"})
    assert form.is_valid(), form.errors
    quotes.save_line(form.build(quotes.create_quote(job)))


@pytest.mark.django_db
def test_product_page_has_tabs_and_saves_back_to_itself(admin_client, product):
    page = admin_client.get(product.get_absolute_url()).content.decode()
    assert "?tab=details" in page and "?tab=files" in page and "Archive" in page
    assert 'name="is_active"' not in page  # the Archive button does that now
    response = admin_client.post(
        product.get_absolute_url(),
        {
            "name": "Walnut cutting board",
            "sku": "CB-1",
            "kind": "made",
            "price": "90",
            "cost": "20",
            "unit": "each",
        },
    )
    assert response["Location"] == product.get_absolute_url()
    product.refresh_from_db()
    assert product.name == "Walnut cutting board" and product.is_active


@pytest.mark.django_db
def test_archived_product_leaves_the_pickers_and_comes_back(admin_client, product):
    admin_client.post(f"/products/{product.pk}/archive/", {"archived": "1"})
    product.refresh_from_db()
    assert not product.is_active
    assert not LINE_FORMS["product"]({"product": str(product.pk), "quantity": "1"}).is_valid()
    admin_client.get("/products/")  # shows the message
    assert product.get_absolute_url() not in admin_client.get("/products/").content.decode()
    page = admin_client.get(product.get_absolute_url()).content.decode()
    assert "Archived" in page and "Restore to the catalog" in page
    admin_client.post(f"/products/{product.pk}/archive/", {"archived": "0"})
    product.refresh_from_db()
    assert product.is_active


@pytest.mark.django_db
def test_unused_product_goes_to_the_trash(admin_client, product):
    assert (
        "Nothing has used" in admin_client.get(f"/products/{product.pk}/delete/").content.decode()
    )
    response = admin_client.post(f"/products/{product.pk}/delete/", {"reason": "Typo"})
    assert response["Location"] == "/products/"
    assert not Product.objects.filter(pk=product.pk).exists()
    assert Product.all_objects.get(pk=product.pk).is_deleted
    # Its SKU is free again.
    Product.objects.create(name="Cutting board", sku="CB-1")


@pytest.mark.django_db
def test_product_on_a_quote_is_archived_not_deleted(admin_client, product):
    _quote(product)
    page = admin_client.get(f"/products/{product.pk}/delete/").content.decode()
    assert "can't be deleted" in page and "Archive it" in page
    admin_client.post(f"/products/{product.pk}/delete/")
    assert Product.objects.filter(pk=product.pk).exists()


@pytest.mark.django_db
def test_deleting_needs_its_own_permission(client, product):
    user = make_user("editor@example.com", "catalog.view", "catalog.edit")
    client.force_login(user)
    page = client.get(product.get_absolute_url()).content.decode()
    assert "Archive" in page and f"/products/{product.pk}/delete/" not in page
    assert client.post(f"/products/{product.pk}/delete/").status_code == 403
    assert Product.objects.filter(pk=product.pk).exists()
