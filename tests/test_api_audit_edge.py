"""The audit log and the portal on Cloudflare over the API (0.9.4): read with
a key holding the right permission, refused without it."""

import json

import pytest
from django.test import Client
from django.utils import timezone

from tests.test_edge import FakeCloudflare, live, worker  # noqa: F401
from tests.test_portal import grace, portal_on, smtp  # noqa: F401
from trammel.accounts.models import ApiKey
from trammel.core import access, audit, modules, secrets
from trammel.core.models import AuditEvent
from trammel.edge import cloudflare, deploy
from trammel.edge.models import EdgeSettings


def _client(user, scopes):
    issued = ApiKey.issue(user, "Test client", scopes)
    token = issued[1] if isinstance(issued, tuple) else issued
    client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")

    def call(method, path, body=None, expect=None):
        response = getattr(client, method.lower())(
            "/api/v1" + path,
            data=json.dumps(body) if body is not None else None,
            content_type="application/json",
        )
        if expect is not None:
            assert response.status_code == expect, (path, response.content[:400])
        return response.json() if response.content else None

    return call


@pytest.mark.django_db
def test_the_audit_log_over_the_api(admin, grace):  # noqa: F811
    audit.record(
        "edge.event_skipped", summary="Skipped a file a customer sent", actor=access.SYSTEM
    )
    audit.record("portal.sign_in", grace, summary="Grace signed in", actor=access.SYSTEM)
    api = _client(admin, ["core.view_audit"])
    found = api("GET", "/audit/?action=edge.event_skipped", expect=200)
    assert found["count"] == 1
    event = found["items"][0]
    assert event["summary"] == "Skipped a file a customer sent" and event["actor_kind"] == "system"
    assert api("GET", f"/audit/{event['id']}", expect=200)["action"] == "edge.event_skipped"
    by_record = api("GET", f"/audit/?entity_id={grace.pk}&q=signed", expect=200)
    assert [e["summary"] for e in by_record["items"]] == ["Grace signed in"]
    today = timezone.localdate().isoformat()
    assert api("GET", f"/audit/?since={today}&actor_kind=system", expect=200)["count"] >= 2
    # Newest first.
    everything = api("GET", "/audit/", expect=200)["items"]
    assert everything == sorted(everything, key=lambda e: e["at"], reverse=True)
    # Without the permission: refused.
    api = _client(admin, ["jobs.view"])
    api("GET", "/audit/", expect=403)
    assert AuditEvent.objects.count() >= 2


@pytest.mark.django_db
def test_the_portal_on_cloudflare_over_the_api(live, admin, monkeypatch):  # noqa: F811
    api = _client(admin, ["edge.manage"])
    status = api("GET", "/edge/status", expect=200)
    assert status["active"] and status["portal_url"].endswith("/portal/")
    assert status["published"] == {} and status["inbox_after"] == 0

    synced = api("POST", "/edge/sync", expect=200)
    assert synced["error"] == "" and synced["pushed"] >= 1
    status = api("GET", "/edge/status", expect=200)
    assert status["published"]["customer"] == 1 and status["last_sync_at"]

    # Deploying needs the token, and runs in the background.
    assert api("POST", "/edge/deploy", expect=422)["detail"][0].startswith("Paste a Cloudflare")
    settings = api(
        "PATCH",
        "/edge/settings",
        {"api_token": "cf-token", "account_id": "0123456789ABCDEF0123456789abcdef"},
        expect=200,
    )
    assert settings["token_set"] and settings["account_id"] == "0123456789abcdef" * 2
    assert "cf-token" not in json.dumps(settings)
    assert secrets.get(deploy.TOKEN_KEY) == "cf-token"
    bad = api("PATCH", "/edge/settings", {"turnstile_own_sitekey": "0x4OWN"}, expect=422)
    assert "turnstile_secret" in bad["detail"]
    api("PATCH", "/edge/settings", {"hostname": "not a host"}, expect=422)
    changed = AuditEvent.objects.filter(summary__startswith="Changed the portal on Cloudflare")
    assert "API token" in changed.get().summary
    started = []
    monkeypatch.setattr("trammel.core.tasks.enqueue", lambda *a, **k: started.append(a))
    assert api("POST", "/edge/deploy", expect=202)["deploying"]
    assert started == [("edge.deploy",)]
    api("POST", "/edge/deploy", expect=422)  # already deploying
    EdgeSettings.objects.filter(pk=1).update(deploying=False)

    stopped = api("POST", "/edge/stop", expect=200)
    assert not stopped["active"] and stopped["published"] == {}
    api("POST", "/edge/sync", expect=422)  # not deployed

    EdgeSettings.objects.filter(pk=1).update(account_id="acct0", database_id="db-uuid")
    fake = FakeCloudflare()
    monkeypatch.setattr(cloudflare, "call", fake.call)
    removed = api("POST", "/edge/remove", expect=200)
    assert not removed["public_url"]
    assert [c[1] for c in fake.calls if c[0] == "DELETE"][:2] == [
        "/accounts/acct0/workers/scripts/trammel-portal?force=true",
        "/accounts/acct0/d1/database/db-uuid",
    ]
    actions = set(AuditEvent.objects.values_list("action", flat=True))
    assert {"edge.deploy", "edge.stop", "edge.remove"} <= actions
    assert json.dumps(stopped)  # (plain JSON)

    # Without the permission, or with the module off: refused.
    _client(admin, ["core.view_audit"])("GET", "/edge/status", expect=403)
    modules.set_enabled("edge", False)
    api("GET", "/edge/status", expect=403)
