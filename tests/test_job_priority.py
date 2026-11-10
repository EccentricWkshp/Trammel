"""Job priority: set on the form and the API, filtered and sorted in the list."""

import datetime as dt
import json

import pytest
from django.test import Client

from trammel.accounts.models import ApiKey
from trammel.core import access
from trammel.jobs import services
from trammel.jobs.models import Job, Priority


@pytest.mark.django_db
def test_form_sets_priority_and_a_missing_one_keeps_the_jobs(admin_client):
    admin_client.post("/jobs/new/", {"title": "Rush sign", "customer": "", "priority": "3"})
    job = Job.objects.get(title="Rush sign")
    assert job.priority == Priority.RUSH and job.is_priority
    admin_client.post(f"/jobs/{job.pk}/edit/", {"title": "Rush sign", "customer": ""})
    job.refresh_from_db()
    assert job.priority == Priority.RUSH
    admin_client.post("/jobs/new/", {"title": "Plain", "customer": ""})
    assert Job.objects.get(title="Plain").priority == Priority.NORMAL


@pytest.mark.django_db
def test_list_filters_and_sorts_by_priority(admin_client):
    today = dt.date.today()
    soon = services.create_job(Job(title="Due first", due_date=today))
    rush = services.create_job(
        Job(title="Rush later", priority=Priority.RUSH, due_date=today + dt.timedelta(days=9))
    )
    services.create_job(Job(title="Low one", priority=Priority.LOW))

    page = admin_client.get("/jobs/?priority=high").content.decode()
    assert "Rush later" in page and "Due first" not in page and "Low one" not in page
    assert 'class="badge priority-rush"' in page

    by_due = admin_client.get("/jobs/").content.decode()
    assert by_due.index(soon.number) < by_due.index(rush.number)
    by_priority = admin_client.get("/jobs/?sort=priority").content.decode()
    assert by_priority.index(rush.number) < by_priority.index(soon.number)

    response = admin_client.get("/jobs/export.csv?status=all&priority=0")
    csv = b"".join(response.streaming_content).decode()
    assert "Low one" in csv and ",Low," in csv and "Rush later" not in csv


@pytest.mark.django_db
def test_api_uses_priority_names(admin):
    token = ApiKey.issue(admin, "t", sorted(access.permissions_of(access.as_actor(admin))))
    token = token[1] if isinstance(token, tuple) else token
    client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")

    def call(method, path, body=None):
        return getattr(client, method)(
            "/api/v1" + path,
            data=json.dumps(body) if body else None,
            content_type="application/json",
        )

    created = call("post", "/jobs/", {"title": "API rush", "priority": "rush"})
    assert created.status_code == 201 and created.json()["priority"] == "rush"
    job_id = created.json()["id"]
    assert call("post", "/jobs/", {"title": "Bad", "priority": "urgent"}).status_code == 422
    assert call("patch", f"/jobs/{job_id}", {"title": "API rush"}).json()["priority"] == "rush"
    assert call("patch", f"/jobs/{job_id}", {"priority": "low"}).json()["priority"] == "low"
    listed = call("get", "/jobs/?priority=low").json()["items"]
    assert [j["id"] for j in listed] == [job_id]
