"""Sign-in codes staff give the customer themselves, instead of emailing one
(FR-PRT-10): long enough not to be guessed, shown once, one-time, expiring, one
at a time, and gone when the customer's access is turned off."""

import datetime as dt
import re

import pytest
from django.test import Client
from django.utils import timezone

from tests.conftest import make_user
from tests.test_portal import _sent_quote, grace, portal_on, smtp  # noqa: F401
from trammel.core import modules
from trammel.core.models import AuditEvent
from trammel.portal import services
from trammel.portal.models import AccessCode
from trammel.quotes.models import Approval


def _make(client, customer) -> str:
    response = client.post(f"/portal/codes/{customer.pk}/")
    assert response["Cache-Control"] == "private, no-store"
    return re.search(r'class="access-code"[^>]*>([0-9A-Z-]+)<', response.content.decode()).group(1)


def _use(code, client=None):
    client = client or Client()
    return client, client.post("/portal/sign-in/access-code/", {"code": code})


@pytest.mark.django_db
def test_staff_make_a_code_the_customer_signs_in_with_once(admin_client, grace, admin):  # noqa: F811
    page = admin_client.get(f"/customers/{grace.pk}/").content.decode()
    assert "Make a sign-in code to give them" in page
    code = _make(admin_client, grace)
    assert re.fullmatch(r"[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}", code)
    # Only a hash is kept.
    assert code.replace("-", "") not in str(list(AccessCode.objects.values()))
    page = admin_client.get(f"/customers/{grace.pk}/").content.decode()
    assert "is waiting to be used" in page and code not in page

    # Typed any old way: lower case, no dashes, O for 0 and I or L for 1.
    typed = code.lower().replace("-", " ").replace("0", "o").replace("1", "l")
    client, response = _use(typed)
    assert response["Location"] == "/portal/"
    assert "Grace Hopper" in client.get("/portal/").content.decode()
    assert AuditEvent.objects.filter(action="portal.code_made", user=admin).exists()
    signed_in = AuditEvent.objects.get(action="portal.sign_in")
    assert "with a code from Ada Admin" in signed_in.summary

    # It works once.
    _, response = _use(code)
    assert response.status_code == 200 and "That code didn" in response.content.decode()


@pytest.mark.django_db
def test_approving_records_who_gave_out_the_code(admin_client, grace, admin):  # noqa: F811
    from trammel.agreements.models import AgreementSettings

    settings = AgreementSettings.load()
    settings.signature_enabled = False
    settings.save()
    revision = _sent_quote(grace, admin)
    client, _ = _use(_make(admin_client, grace))
    page = client.get(f"/portal/quotes/{revision.pk}/").content.decode()
    terms = re.search(r'name="terms" value="([0-9a-f]+)"', page)
    data = {"answer": "approve", "name": "Grace Hopper", "accept": "1"}
    if terms:
        data["terms"] = terms.group(1)
    client.post(f"/portal/quotes/{revision.pk}/respond/", data)
    revision.refresh_from_db()
    assert revision.status == "approved"
    assert "signed in with a code from Ada Admin" in Approval.objects.get(revision=revision).notes


@pytest.mark.django_db
def test_a_new_code_cancel_or_access_off_ends_the_old_one(admin_client, grace):  # noqa: F811
    first = _make(admin_client, grace)
    second = _make(admin_client, grace)
    assert _use(first)[1].status_code == 200  # replaced
    admin_client.post(f"/portal/codes/{grace.pk}/cancel/")
    assert _use(second)[1].status_code == 200  # canceled
    third = _make(admin_client, grace)
    services.set_access(grace, False, user=None)
    services.set_access(grace, True, user=None)
    assert _use(third)[1].status_code == 200  # access was turned off
    assert AuditEvent.objects.filter(action="portal.code_canceled").count() == 1


@pytest.mark.django_db
def test_codes_expire_and_sessions_end_with_access(admin_client, grace):  # noqa: F811
    code = _make(admin_client, grace)
    AccessCode.objects.update(expires_at=timezone.now() - dt.timedelta(minutes=1))
    assert _use(code)[1].status_code == 200
    client, response = _use(_make(admin_client, grace))
    assert response["Location"] == "/portal/"
    services.set_access(grace, False, user=None)
    assert client.get("/portal/")["Location"] == "/portal/sign-in/"


@pytest.mark.django_db
def test_wrong_codes_count_toward_the_limit_per_ip(admin_client, grace):  # noqa: F811
    code = _make(admin_client, grace)
    client = Client()
    for _ in range(services.FAILURES_PER_IP):
        _use("AAAA-BBBB-CCCC", client)
    # Even the right code is refused once the limit is reached.
    assert _use(code, client)[1].status_code == 200
    assert AccessCode.objects.get().used_at is None


@pytest.mark.django_db
def test_codes_work_without_email_set_up(admin_client, customer, setup_done, settings):
    settings.TRAMMEL_EMAIL_BACKEND = ""
    modules.set_enabled("portal", True)
    services.set_access(customer, True, user=None)
    page = Client().get("/portal/sign-in/").content.decode()
    assert "Send me a code" not in page and "Enter your code" in page
    panel = admin_client.get(f"/customers/{customer.pk}/").content.decode()
    assert "Make a code to give them instead" in panel
    _, response = _use(_make(admin_client, customer))
    assert response["Location"] == "/portal/"


@pytest.mark.django_db
def test_only_staff_who_manage_portal_access_make_codes(client, grace):  # noqa: F811
    client.force_login(make_user("viewer@example.com", "customers.view"))
    assert "Make a sign-in code" not in client.get(f"/customers/{grace.pk}/").content.decode()
    assert client.post(f"/portal/codes/{grace.pk}/").status_code == 403
    assert not AccessCode.objects.exists()


@pytest.mark.django_db
def test_no_code_without_portal_access(admin_client, customer, portal_on):  # noqa: F811
    response = admin_client.post(f"/portal/codes/{customer.pk}/")
    assert response["Location"] == f"/customers/{customer.pk}/"
    assert not AccessCode.objects.exists()
