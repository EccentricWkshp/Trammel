"""Staff viewing the portal as a customer would: exactly what the customer
sees, read-only, from the customer's page, for staff who manage portal access."""

import pytest
from django.contrib.messages import get_messages

from tests.conftest import make_user
from tests.test_portal import _sent_quote, grace, portal_on, smtp  # noqa: F401
from trammel.core.models import AuditEvent
from trammel.customers.models import Customer


def _preview(client, customer):
    return client.post(f"/portal/preview/{customer.pk}/")


@pytest.mark.django_db
def test_staff_view_the_portal_as_the_customer_sees_it(admin_client, grace, admin):  # noqa: F811
    revision = _sent_quote(grace, admin)
    page = admin_client.get(f"/customers/{grace.pk}/").content.decode()
    assert "View the portal as this customer" in page
    response = _preview(admin_client, grace)
    assert response.status_code == 302 and response["Location"] == "/portal/"
    page = admin_client.get("/portal/").content.decode()
    assert "viewing the portal as <strong>Grace Hopper</strong>" in page
    assert revision.quote.number in page and "Sign out" not in page
    event = AuditEvent.objects.get(action="portal.preview")
    assert event.user == admin and event.entity_id == str(grace.pk)
    # The answer buttons are there to see, but off.
    page = admin_client.get(f"/portal/quotes/{revision.pk}/").content.decode()
    assert "In this preview the buttons are off" in page
    assert page.count(" disabled>") == 2


@pytest.mark.django_db
def test_nothing_is_done_in_the_customers_name(admin_client, grace, admin):  # noqa: F811
    revision = _sent_quote(grace, admin)
    _preview(admin_client, grace)
    url = f"/portal/quotes/{revision.pk}/"
    for answer in ("approve", "decline"):
        response = admin_client.post(
            f"{url}respond/", {"answer": answer, "name": "Grace Hopper"}, HTTP_REFERER=url
        )
        assert response.status_code == 302 and response["Location"] == url
        assert "nothing can be approved, declined or changed" in str(
            list(get_messages(response.wsgi_request))[-1]
        )
    revision.refresh_from_db()
    assert revision.status == "sent"


@pytest.mark.django_db
def test_the_preview_sees_only_that_customers_records(admin_client, grace, admin):  # noqa: F811
    other = Customer.objects.create(name="Alan Turing", email="alan@example.com")
    theirs = _sent_quote(other, admin, title="Desk")
    _preview(admin_client, grace)
    assert admin_client.get(f"/portal/quotes/{theirs.pk}/").status_code == 404
    assert "Desk" not in admin_client.get("/portal/").content.decode()


@pytest.mark.django_db
def test_stopping_goes_back_to_the_customer(admin_client, grace):  # noqa: F811
    _preview(admin_client, grace)
    response = admin_client.post("/portal/preview/stop/")
    assert response["Location"] == f"/customers/{grace.pk}/"
    assert admin_client.get("/portal/")["Location"] == "/portal/sign-in/"


@pytest.mark.django_db
def test_it_works_before_the_customer_has_portal_access(admin_client, customer, portal_on):  # noqa: F811
    _preview(admin_client, customer)
    page = admin_client.get("/portal/").content.decode()
    assert "Their portal access is off, so they can't sign in yet." in page


@pytest.mark.django_db
def test_only_staff_who_manage_portal_access_can(client, grace):  # noqa: F811
    # Seeing the customer isn't enough: the portal shows their quotes and invoices.
    viewer = make_user("viewer@example.com", "customers.view")
    client.force_login(viewer)
    assert "View the portal as" not in client.get(f"/customers/{grace.pk}/").content.decode()
    assert _preview(client, grace).status_code == 403

    manager = make_user("manager@example.com", "customers.view", "portal.manage", name="Max")
    client.force_login(manager)
    assert _preview(client, grace)["Location"] == "/portal/"
    assert client.get("/portal/").status_code == 200
    # Losing the permission ends the preview at once.
    manager.roles.clear()
    assert client.get("/portal/")["Location"] == "/portal/sign-in/"


@pytest.mark.django_db
def test_signed_out_visitors_cant_start_one(client, grace):  # noqa: F811
    assert _preview(client, grace)["Location"].startswith("/accounts/login/")
    assert client.get("/portal/")["Location"] == "/portal/sign-in/"
