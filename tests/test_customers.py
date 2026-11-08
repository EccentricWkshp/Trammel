"""FR-CUS-1..4 through the web UI and the API (FR-API-1)."""

import json

import pytest

from tests.conftest import make_user
from trammel.accounts.models import ApiKey
from trammel.core.models import AuditEvent
from trammel.customers.models import Customer


@pytest.mark.django_db
def test_list_search_and_detail(staff_client, customer):
    Customer.objects.create(name="Alan Turing", company="Bletchley")
    page = staff_client.get("/customers/?q=bletch").content.decode()
    assert "Alan Turing" in page and "Grace Hopper" not in page
    # Substring matching on identifiers, e.g. the end of a phone number.
    assert "Grace Hopper" in staff_client.get("/customers/?q=0100").content.decode()
    assert staff_client.get(f"/customers/{customer.pk}/").status_code == 200


@pytest.mark.django_db
def test_create_is_audited_and_edit_needs_permission(staff_client, customer):
    response = staff_client.post(
        "/customers/new/", {"kind": "company", "name": "Acme Fabrication", "email": "ops@acme.test"}
    )
    created = Customer.objects.get(name="Acme Fabrication")
    assert response.url == f"/customers/{created.pk}/"
    assert AuditEvent.objects.filter(action="create", entity_id=str(created.pk)).exists()
    # Staff may view and create, not edit or delete.
    assert staff_client.get(f"/customers/{customer.pk}/edit/").status_code == 403
    assert staff_client.post(f"/customers/{customer.pk}/delete/").status_code == 403


@pytest.mark.django_db
def test_tax_exempt_requires_a_certificate_number(admin_client):
    response = admin_client.post(
        "/customers/new/", {"kind": "company", "name": "Reseller", "tax_exempt": "on"}
    )
    assert response.status_code == 200
    assert not Customer.objects.filter(name="Reseller").exists()


@pytest.mark.django_db
def test_contacts_and_addresses_keep_one_default(admin_client, customer):
    base = f"/customers/{customer.pk}/address/new/"
    data = {"line1": "1 Main St", "city": "Tacoma", "country": "us", "is_default_billing": "on"}
    admin_client.post(base, data)
    admin_client.post(base, {**data, "line1": "2 Side St"})
    defaults = list(
        customer.addresses.filter(is_default_billing=True).values_list("line1", flat=True)
    )
    assert defaults == ["2 Side St"]
    assert customer.addresses.first().country == "US"
    assert admin_client.get(f"/customers/{customer.pk}/bogus/new/").status_code == 404


@pytest.mark.django_db
def test_soft_delete_cascades_and_placeholder_is_protected(admin_client, customer):
    customer.contacts.create(name="Assistant")
    admin_client.post(f"/customers/{customer.pk}/delete/", {"reason": "duplicate"})
    assert not Customer.objects.filter(pk=customer.pk).exists()
    assert Customer.all_objects.get(pk=customer.pk).is_deleted
    assert customer.contacts(manager="all_objects").get().is_deleted
    assert AuditEvent.objects.get(action="delete").reason == "duplicate"

    walk_in = Customer.placeholder()
    admin_client.post(f"/customers/{walk_in.pk}/delete/")
    assert Customer.objects.filter(pk=walk_in.pk).exists()


@pytest.mark.django_db
def test_csv_export_neutralizes_formulas(admin_client, setup_done):
    Customer.objects.create(name='=HYPERLINK("evil")')
    body = b"".join(admin_client.get("/customers/export.csv").streaming_content).decode()
    assert "'=HYPERLINK" in body


@pytest.mark.django_db
def test_api_with_key(client, setup_done, customer):
    user = make_user("api@example.com", "customers.view", "customers.create", "accounts.api_keys")
    _key, token = ApiKey.issue(user, "script", ["customers.view", "customers.create"])
    auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    listing = client.get("/api/v1/customers/", **auth).json()
    assert listing["count"] == 1 and listing["items"][0]["name"] == "Grace Hopper"

    created = client.post(
        "/api/v1/customers/",
        json.dumps({"name": "Via API"}),
        content_type="application/json",
        **auth,
    )
    assert created.status_code == 201
    event = AuditEvent.objects.get(action="create", entity_label="Via API")
    assert event.actor_kind == "api_key" and event.source == "api" and event.user == user

    patched = client.patch(
        f"/api/v1/customers/{customer.pk}",
        json.dumps({"phone": "1"}),
        content_type="application/json",
        **auth,
    )
    assert patched.status_code == 403
    assert (
        client.get("/api/v1/customers/", HTTP_AUTHORIZATION="Bearer trm_bad_token").status_code
        == 401
    )


@pytest.mark.django_db
def test_api_session_requires_csrf_for_writes(admin, setup_done):
    from django.test import Client

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(admin)
    assert csrf_client.get("/api/v1/me").json()["is_admin"] is True
    response = csrf_client.post(
        "/api/v1/customers/", json.dumps({"name": "x"}), content_type="application/json"
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_api_validation_errors_are_422(admin_client):
    response = admin_client.post(
        "/api/v1/customers/",
        json.dumps({"name": "X", "tax_exempt": True}),
        content_type="application/json",
    )
    assert response.status_code == 422
