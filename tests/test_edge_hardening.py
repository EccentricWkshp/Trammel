"""Hardening the edge portal (0.9.2): closed jobs coming off Cloudflare, a new
Worker database caught up, an event that keeps failing skipped, a banner when
the sync has been failing, the bot check set up, and removing it all."""

import datetime as dt

import pytest
from django.utils import timezone

from tests.test_edge import URL, FakeCloudflare, live, worker  # noqa: F401
from tests.test_portal import _sent_quote, grace, portal_on, smtp  # noqa: F401
from trammel.core import modules, secrets
from trammel.core.models import AuditEvent
from trammel.edge import cloudflare, deploy, sync
from trammel.edge.models import EdgeSettings, Published
from trammel.jobs.models import Job


@pytest.mark.django_db
def test_a_closed_job_comes_off_cloudflare_after_the_days_kept(live, grace, admin):  # noqa: F811
    revision = _sent_quote(grace, admin)
    job = revision.quote.job
    sync.run()
    assert str(job.pk) in live.of("job") and str(revision.pk) in live.of("quote")
    Job.objects.filter(pk=job.pk).update(
        status="cancelled", updated_at=timezone.now() - dt.timedelta(days=30)
    )
    sync.run()
    assert str(job.pk) in live.of("job")  # kept 90 days
    EdgeSettings.objects.filter(pk=1).update(keep_closed_days=10)
    sync.run()
    assert str(job.pk) not in live.of("job") and str(revision.pk) not in live.of("quote")
    assert live.of("customer")  # the customer stays


@pytest.mark.django_db
def test_a_new_worker_database_gets_everything_again(live, grace, admin):  # noqa: F811
    _sent_quote(grace, admin)
    sync.run()
    assert EdgeSettings.load().instance == "db-1"
    pushed = len(live.records)
    EdgeSettings.objects.filter(pk=1).update(inbox_after=40)
    # The database deleted and made afresh: empty, its inbox numbered from 1.
    live.records.clear()
    live.objects.clear()
    live.instance = "db-2"
    sync.run()
    assert len(live.records) == pushed
    row = EdgeSettings.load()
    assert row.instance == "db-2" and row.inbox_after == 0
    # Records lost without a new database (half or more gone): put back too.
    live.records.clear()
    sync.run()
    assert len(live.records) == pushed


@pytest.mark.django_db
def test_an_event_that_keeps_failing_is_skipped(live, grace, monkeypatch):  # noqa: F811
    calls = []

    def broken(event):
        calls.append(event["seq"])
        raise RuntimeError("cannot apply")

    monkeypatch.setitem(sync.HANDLERS, "signed_in", broken)
    live.inbox = [
        {
            "seq": 1,
            "kind": "signed_in",
            "at": timezone.now().isoformat(),
            "ip": "",
            "customer": None,
            "data": {},
        },
        {
            "seq": 2,
            "kind": "code_sent",
            "at": timezone.now().isoformat(),
            "ip": "",
            "customer": str(grace.pk),
            "data": {"email": "grace@example.com", "customer": str(grace.pk)},
        },
    ]
    for _ in range(4):
        assert "cannot apply" in sync.run()["error"]
    assert EdgeSettings.load().inbox_after == 0
    result = sync.run()  # the fifth try: skipped, and what follows applied
    assert "error" not in result and EdgeSettings.load().inbox_after == 2
    assert calls == [1, 1, 1, 1, 1]
    assert AuditEvent.objects.filter(action="edge.event_skipped").exists()
    assert EdgeSettings.load().failures == {}


@pytest.mark.django_db
def test_admins_hear_when_the_sync_has_been_failing(live, admin_client):  # noqa: F811
    assert "synced since" not in admin_client.get("/").content.decode()
    EdgeSettings.objects.filter(pk=1).update(
        last_error="Couldn't reach the edge portal: timed out",
        last_sync_at=timezone.now() - dt.timedelta(hours=1),
    )
    page = admin_client.get("/").content.decode()
    assert "synced since" in page and "timed out" in page and "/settings/edge-portal/" in page


@pytest.mark.django_db
def test_the_bot_check_is_set_up_or_gone_without(monkeypatch, portal_on, worker, smtp):  # noqa: F811
    modules.set_enabled("edge", True)
    secrets.put(deploy.TOKEN_KEY, "cf-token")
    fake = FakeCloudflare()
    monkeypatch.setattr(cloudflare, "call", fake.call)
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
    deploy.run_deploy()
    row = EdgeSettings.load()
    assert row.active and row.turnstile_sitekey == "0x4AAA"
    assert secrets.get(deploy.TURNSTILE_KEY) == "ts-secret"
    widget = next(c for c in fake.calls if c[1].endswith("/challenges/widgets"))
    assert widget[2] == {
        "name": "trammel-portal",
        "domains": ["trammel-portal.shop.workers.dev"],
        "mode": "managed",
    }
    secret = next(c for c in fake.calls if c[1].endswith("/scripts/trammel-portal/secrets"))
    assert secret[2] == {"name": "TURNSTILE_SECRET", "text": "ts-secret", "type": "secret_text"}
    assert worker.records[("config", "site")]["data"]["turnstile"] == "0x4AAA"

    # Without the permission: deployed anyway, without the bot check.
    EdgeSettings.objects.filter(pk=1).update(turnstile_sitekey="")
    fake.turnstile_refused = True
    deploy.run_deploy()
    row = EdgeSettings.load()
    assert row.active and not row.turnstile_sitekey
    assert any("without the bot check" in step["text"] for step in row.deploy_log)


@pytest.mark.django_db
def test_removing_it_from_cloudflare(monkeypatch, live, admin_client):  # noqa: F811
    secrets.put(deploy.TOKEN_KEY, "cf-token")
    EdgeSettings.objects.filter(pk=1).update(
        account_id="acct0",
        database_id="db-uuid",
        bucket="trammel-portal",
        turnstile_sitekey="0x4AAA",
    )
    sync.run()
    fake = FakeCloudflare()
    monkeypatch.setattr(cloudflare, "call", fake.call)
    response = admin_client.post("/settings/edge-portal/remove/")
    assert response.status_code == 302
    deleted = [c[1] for c in fake.calls if c[0] == "DELETE"]
    assert deleted == [
        "/accounts/acct0/workers/scripts/trammel-portal?force=true",
        "/accounts/acct0/d1/database/db-uuid",
        "/accounts/acct0/r2/buckets/trammel-portal",
        "/accounts/acct0/challenges/widgets/0x4AAA",
    ]
    row = EdgeSettings.load()
    assert not row.active and not row.public_url and not row.database_id
    assert not live.records and not Published.objects.exists()
    assert URL  # (the address it had)
