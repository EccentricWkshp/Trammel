"""The customer portal's staff side over the API (0.9.4, FR-API-1): access,
codes to give out, reviewing the files customers send, and what they may send."""

import base64
import json
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from tests.test_portal import grace, portal_on, smtp  # noqa: F401
from trammel.accounts.models import ApiKey
from trammel.core import modules
from trammel.core.models import AuditEvent
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.portal import uploads
from trammel.portal.models import AccessCode, PortalSettings, Upload

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _client(user, scopes):
    issued = ApiKey.issue(user, "Test client", scopes)
    token = issued[1] if isinstance(issued, tuple) else issued
    client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")

    def call(method, path, body=None, expect=None, raw=False):
        response = getattr(client, method.lower())(
            "/api/v1" + path,
            data=json.dumps(body) if body is not None else None,
            content_type="application/json",
        )
        if expect is not None:
            assert response.status_code == expect, (path, response.content[:400])
        if raw:
            return response
        return response.json() if response.content else None

    return call


@pytest.fixture
def sending(grace):  # noqa: F811
    modules.set_enabled("files", True)
    row = PortalSettings.load()
    row.uploads_enabled = True
    row.save()
    return grace


def _send(customer, name="sketch.png", job=None):
    visitor = SimpleNamespace(customer=customer, name="Grace")
    made, problems = uploads.send(
        visitor,
        [SimpleUploadedFile(name, PNG, content_type="image/png")],
        job=job,
        note="Like this",
    )
    assert made, problems
    return made[0]


@pytest.mark.django_db
def test_access_and_codes_over_the_api(admin, customer, portal_on):  # noqa: F811
    api = _client(admin, ["customers.view", "portal.manage"])
    path = f"/portal/customers/{customer.pk}"
    assert api("GET", f"{path}/access", expect=200)["enabled"] is False
    api("POST", f"{path}/codes", expect=422)  # access first

    turned_on = api("PUT", f"{path}/access", {"enabled": True}, expect=200)
    assert turned_on["enabled"] and turned_on["portal_url"]
    assert [s["email"] for s in turned_on["signers"]] == [customer.email.lower()]

    made = api("POST", f"{path}/codes", expect=201)
    assert len(made["code"]) == 14 and made["code"].count("-") == 2
    assert made["sign_in_url"].endswith("/portal/sign-in/access-code/")
    assert AccessCode.objects.get(customer=customer).is_live
    waiting = api("GET", f"{path}/access", expect=200)["waiting_code"]
    assert waiting and waiting["created_by"]
    assert made["code"] not in json.dumps(api("GET", f"{path}/access", expect=200))

    assert api("DELETE", f"{path}/codes", expect=200) == {"canceled": 1}
    assert api("GET", f"{path}/access", expect=200)["waiting_code"] is None
    api("POST", f"{path}/codes", expect=201)
    turned_off = api("PUT", f"{path}/access", {"enabled": False}, expect=200)
    assert not turned_off["enabled"] and not AccessCode.objects.filter(canceled_at=None).exists()
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"portal.access_on", "portal.code_made", "portal.access_off"} <= actions

    # Seeing it needs only the customer; changing it needs portal.manage.
    reader = _client(admin, ["customers.view"])
    reader("GET", f"{path}/access", expect=200)
    reader("PUT", f"{path}/access", {"enabled": True}, expect=403)
    reader("POST", f"{path}/codes", expect=403)


@pytest.mark.django_db
def test_reviewing_files_customers_sent_over_the_api(admin, sending):
    job = jobs.create_job(Job(title="Sign", customer=sending))
    first = _send(sending, job=job)
    second = _send(sending, name="other.png")
    third = _send(sending, name="third.png")
    api = _client(admin, ["customers.view", "files.view", "files.upload", "jobs.view"])

    waiting = api("GET", "/portal/uploads?status=pending", expect=200)
    assert waiting["count"] == 3
    assert [u["id"] for u in waiting["items"]] == [str(first.pk), str(second.pk), str(third.pk)]
    item = waiting["items"][0]
    assert item["original_name"] == "sketch.png" and item["job_number"] == job.number
    assert item["note"] == "Like this" and item["type"] == "image/png"

    response = api("GET", f"/portal/uploads/{first.pk}/file", expect=200, raw=True)
    assert b"".join(response.streaming_content) == PNG
    response = api("GET", f"/files/revisions/{item['revision_id']}/download", expect=200, raw=True)
    assert b"".join(response.streaming_content) == PNG

    accepted = api(
        "POST",
        f"/portal/uploads/{first.pk}/accept",
        {"target": str(job.pk), "title": "Sign sketch", "shared": False},
        expect=200,
    )
    assert accepted["status"] == "accepted" and accepted["title"] == "Sign sketch"
    assert accepted["attached_to"] == str(job) and accepted["shared_with_customer"] is False
    assert accepted["reviewed_by"]
    files = api("GET", f"/files/jobs.job/{job.pk}", expect=200)
    assert [f["title"] for f in files] == ["Sign sketch"]
    api("POST", f"/portal/uploads/{first.pk}/decline", {"reason": "x"}, expect=422)  # reviewed

    onto_customer = api("POST", f"/portal/uploads/{second.pk}/accept", {}, expect=200)
    assert onto_customer["attached_to"] == str(sending)
    other = Job.objects.create(title="Elsewhere", customer=admin_customer(sending))
    api("POST", f"/portal/uploads/{third.pk}/accept", {"target": str(other.pk)}, expect=422)

    declined = api(
        "POST", f"/portal/uploads/{third.pk}/decline", {"reason": "Too blurry"}, expect=200
    )
    assert declined["status"] == "declined" and declined["reason"] == "Too blurry"
    assert declined["revision_id"] is None
    api("GET", f"/portal/uploads/{third.pk}/file", expect=404)
    assert Upload.objects.filter(status="pending").count() == 0
    everything = api("GET", f"/portal/uploads?customer={sending.pk}", expect=200)
    assert everything["count"] == 3

    # Without files.upload: refused.
    _client(admin, ["customers.view"])("GET", "/portal/uploads", expect=403)


def admin_customer(other):
    """A customer that isn't `other`."""
    from trammel.customers.models import Customer

    return Customer.objects.create(name="Someone else", email="else@example.com")


@pytest.mark.django_db
def test_what_customers_may_send_over_the_api(admin, sending):
    api = _client(admin, ["portal.manage"])
    shown = api("GET", "/portal/settings", expect=200)
    assert shown["portal_on"] and shown["uploads_enabled"] and "pdf" in shown["kinds"]
    changed = api("PATCH", "/portal/settings", {"upload_limit_mb": 5}, expect=200)
    assert changed["upload_limit_mb"] == 5 and changed["uploads_enabled"]
    off = api("PATCH", "/portal/settings", {"uploads_enabled": False}, expect=200)
    assert not off["uploads_enabled"] and off["upload_limit_mb"] == 5
    assert (
        "upload_limit_mb"
        in api("PATCH", "/portal/settings", {"upload_limit_mb": 0}, expect=422)["detail"]
    )
    _client(admin, ["jobs.view"])("GET", "/portal/settings", expect=403)
    _client(admin, ["files.upload"])(
        "PATCH", "/portal/settings", {"upload_limit_mb": 9}, expect=403
    )
