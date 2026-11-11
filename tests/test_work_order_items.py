"""Resold products on a work order are items to include, checked off rather than
worked on; operations from quote lines say how many when it's more than one."""

from decimal import Decimal

import pytest

from trammel.catalog import services as catalog
from trammel.catalog.models import Product
from trammel.core.lifecycle import TransitionError
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.quotes.models import QuoteLine
from trammel.workorders import services as wos
from trammel.workorders.models import IncludedItem

D = Decimal


def _adhoc(revision, description, price, quantity="1", unit="each"):
    form = LINE_FORMS["adhoc"](
        {"description": description, "quantity": quantity, "unit": unit, "unit_price": price}
    )
    assert form.is_valid(), form.errors
    return quotes.save_line(form.build(revision))


def _product(revision, product, quantity):
    form = LINE_FORMS["product"]({"product": str(product.pk), "quantity": quantity})
    assert form.is_valid(), form.errors
    return quotes.save_line(form.build(revision))


@pytest.fixture
def butter(setup_done):
    return catalog.save_product(Product(name="Board butter, 4 oz", kind="resold", price=D("12")))


@pytest.fixture
def wo(customer, admin, butter):
    job = jobs.create_job(Job(title="Dining table", customer=customer))
    revision = quotes.create_quote(job)
    _adhoc(revision, "Build and finish", "1400")
    _adhoc(revision, "Matching bench", "450", quantity="2")
    _adhoc(revision, "Tabletop veneer", "15", quantity="12.5", unit="sq_ft")
    _product(revision, butter, "3")
    quotes.send(revision, actor=admin)
    quotes.approve(revision, actor=admin, approved_by_name="Grace", accept_agreement=True)
    revision.refresh_from_db()
    return wos.create(job, revision=revision)


@pytest.mark.django_db
def test_resold_products_are_items_to_include_not_operations(wo, butter):
    names = list(wo.operations.values_list("name", flat=True))
    assert names == ["Build and finish", "Matching bench × 2", "Tabletop veneer × 12.5 ft²"]
    item = wo.items.get()
    assert (item.product, item.description, item.quantity) == (butter, "Board butter, 4 oz", 3)
    # Time and materials measure themselves: no count added.
    assert wos.operation_name(QuoteLine(kind="labor", description="Design", quantity=3)) == (
        "Design"
    )


@pytest.mark.django_db
def test_completing_waits_for_the_items(wo, admin):
    for op in wo.operations.all():
        wos.set_operation(op, "done", user=admin)
    with pytest.raises(TransitionError, match="1 item"):
        wos.transition(wo, "complete", actor=admin)
    item = wo.items.get()
    wos.set_item(item, "skipped", user=admin)
    wos.set_item(item, "pending", user=admin)
    wos.set_item(item, "done", user=admin)
    item.refresh_from_db()
    assert item.completed_by == admin
    wos.transition(wo, "complete", actor=admin)
    wo.refresh_from_db()
    assert wo.status == "complete"


@pytest.mark.django_db
def test_checked_off_on_the_page_the_floor_app_and_the_api(admin_client, wo):
    item = wo.items.get()
    page = admin_client.get(wo.get_absolute_url()).content.decode()
    assert "Items to include" in page and "1 left" in page
    admin_client.post(f"/work-orders/{wo.pk}/items/{item.pk}/", {"state": "done"})
    item.refresh_from_db()
    assert item.status == "done"
    page = admin_client.get(f"/floor/wo/{wo.pk}/").content.decode()
    assert "Items to include" in page and "Included ↺" in page
    page = admin_client.get(f"/work-orders/{wo.pk}/traveler/").content.decode()
    assert "Items to include" in page and "Board butter, 4 oz" in page
    body = admin_client.post(
        f"/api/v1/work-orders/{wo.pk}/items/{item.pk}",
        {"state": "pending"},
        content_type="application/json",
    ).json()
    assert [(x["description"], x["status"]) for x in body["items"]] == [
        ("Board butter, 4 oz", "pending")
    ]
    admin_client.post(f"/work-orders/{wo.pk}/items/{item.pk}/delete/")
    assert not wo.items.exists()


@pytest.mark.django_db
def test_a_change_order_adds_its_items(wo, admin, butter):
    change = quotes.create_quote(wo.job)
    _product(change, butter, "2")
    quotes.send(change, actor=admin)
    quotes.approve(change, actor=admin, approved_by_name="Grace", accept_agreement=True)
    added = wo.items.get(source_revision=change)
    assert added.quantity == 2
    # Nothing to make: the change order's operation is done already.
    op = wo.operations.get(source_revision=change)
    assert op.status == "done" and not op.children.exists()
    assert IncludedItem.objects.filter(work_order=wo).count() == 2
