"""FR-ADM-1: setup page on an empty database, once, no default login."""

import pytest
from django.test import override_settings

from trammel.accounts.models import User
from trammel.core.models import AuditEvent, BusinessProfile, SystemSettings

SETUP = {
    "business_name": "Eccentric Workshop",
    "full_name": "Ada Lovelace",
    "email": "Ada@Example.com",
    "password": "a long enough passphrase",
    "password_confirm": "a long enough passphrase",
    "timezone": "America/Los_Angeles",
    "unit_system": "imperial",
    "currency": "USD",
}


@pytest.mark.django_db
def test_every_page_redirects_to_setup_until_done(client):
    assert client.get("/").url == "/setup/"
    assert client.get("/customers/").url == "/setup/"
    assert client.get("/api/v1/me").status_code == 503
    assert client.get("/healthz").status_code == 200


@pytest.mark.django_db
def test_setup_creates_admin_and_business_then_disappears(client):
    response = client.post("/setup/", SETUP)
    assert response.status_code == 302 and response.url == "/"

    admin = User.objects.get()
    assert admin.email == "ada@example.com" and admin.is_admin
    assert admin.password.startswith("md5$")  # test hasher; Argon2 in production
    business = BusinessProfile.load()
    assert (business.name, business.timezone, business.currency) == (
        "Eccentric Workshop",
        "America/Los_Angeles",
        "USD",
    )
    assert SystemSettings.load().setup_completed_at is not None
    assert AuditEvent.objects.filter(action="setup.complete", user=admin).exists()

    # Signed in, and the setup page no longer exists — even for anonymous users.
    assert client.get("/").status_code == 200
    client.logout()
    assert client.get("/setup/").status_code == 404
    assert client.post("/setup/", SETUP).status_code == 404
    assert User.objects.count() == 1


@pytest.mark.django_db
def test_setup_rejects_weak_or_mismatched_passwords(client):
    response = client.post("/setup/", {**SETUP, "password": "short", "password_confirm": "short"})
    assert response.status_code == 200
    assert not User.objects.exists()
    response = client.post("/setup/", {**SETUP, "password_confirm": "something else entirely"})
    assert "do not match" in response.content.decode()


@pytest.mark.django_db
@override_settings(SETUP_TOKEN="let-me-in")
def test_setup_token_when_configured(client):
    response = client.post("/setup/", {**SETUP, "setup_token": "wrong"})
    assert response.status_code == 200 and not User.objects.exists()
    response = client.post("/setup/", {**SETUP, "setup_token": "let-me-in"})
    assert response.status_code == 302 and User.objects.count() == 1


@pytest.mark.django_db
def test_setup_page_warns_without_tls(client):
    assert "not encrypted" in client.get("/setup/").content.decode()
