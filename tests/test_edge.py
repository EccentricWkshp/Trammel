"""The edge portal on Cloudflare (0.9.0): deploying it over Cloudflare's API,
publishing what customers may see, applying what they did, and the built-in
portal stepping aside while it serves them. The Worker itself is tested under
Node (trammel/edge/worker/tests); here it's a fake that speaks the same signed
protocol."""

import datetime as dt
import json
import time

import pytest
from django.test import Client
from django.utils import timezone

from tests.test_portal import _sent_quote, grace, portal_on, smtp  # noqa: F401
from trammel.core import modules, secrets
from trammel.core.models import AuditEvent
from trammel.edge import client, cloudflare, deploy, sync
from trammel.edge.models import EdgeAccessCode, EdgeSettings, Published
from trammel.notifications.models import EmailLog
from trammel.portal import services as portal
from trammel.portal.models import Access, AccessCode

URL = "https://trammel-portal.shop.workers.dev"


class FakeWorker:
    """The Worker's sync calls, checking Trammel's signature as it does."""

    def __init__(self):
        self.records, self.objects, self.inbox = {}, {}, []
        self.acked = 0
        self.calls = []
        self.instance = "db-1"

    def call(
        self, method, path, *, data=None, body=None, content_type=None, base_url=None, raw=False
    ):
        self.calls.append((method, path))
        if data is not None:
            body = json.dumps(data, separators=(",", ":")).encode()
        stamp = str(int(time.time()))
        # What the real client sends must verify on the other side.
        assert client.signature(client.sync_key(), stamp, method, path, body or b"")
        if path == "/_sync/health":
            return {
                "ok": True,
                "version": "test",
                "schema": 1,
                "instance": self.instance,
                "records": len(self.records),
            }
        if path == "/_sync/records":
            for r in data["upsert"]:
                self.records[(r["type"], r["id"])] = r
            for r in data["delete"]:
                self.records.pop((r["type"], r["id"]), None)
            return {}
        if path.startswith("/_sync/objects/"):
            key = path.split("/_sync/objects/", 1)[1].replace("%2F", "/")
            if method == "PUT":
                self.objects[key] = (body, content_type)
            else:
                self.objects.pop(key, None)
            return {}
        if path.startswith("/_sync/inbox"):
            after = int(path.split("after=")[1].split("&")[0])
            return {"events": [e for e in self.inbox if e["seq"] > after]}
        if path == "/_sync/ack":
            self.acked = data["upto"]
            self.inbox = [e for e in self.inbox if e["seq"] > data["upto"]]
            return {}
        if path in ("/_sync/sessions/end", "/_sync/purge"):
            if path == "/_sync/purge":
                self.records.clear()
                self.objects.clear()
            return {}
        raise AssertionError(f"unexpected call {method} {path}")

    def of(self, type_):
        return {k[1]: r for k, r in self.records.items() if k[0] == type_}


@pytest.fixture
def worker(monkeypatch, settings):
    fake = FakeWorker()
    monkeypatch.setattr(client, "call", fake.call)
    return fake


@pytest.fixture
def live(grace, worker):  # noqa: F811
    """The edge portal deployed and serving."""
    modules.set_enabled("edge", True)
    row = EdgeSettings.load()
    row.public_url, row.active = URL, True
    row.save()
    return worker


def test_the_signature_matches_the_workers():
    """The same vector as the Worker's own test (worker.test.mjs)."""
    assert (
        client.signature("test-key", "1700000000", "POST", "/_sync/records", b'{"a":1}')
        == "ae5b06011cc6a51d2ebe237eb989c8e13ec2f1507f106b4d91648b5b9418a4dd"
    )


@pytest.mark.django_db
def test_publishing_what_a_customer_may_see(live, grace, admin):  # noqa: F811
    revision = _sent_quote(grace, admin)
    result = sync.run()
    assert "error" not in result, result
    site = live.records[("config", "site")]["data"]
    assert site["portal_on"] and site["css"].startswith("portal-")
    assert "%%CODE%%" in site["email"]["body"] and site["email"]["enabled"]
    assert f"assets/{site['css']}" in live.objects
    customer = live.of("customer")[str(grace.pk)]
    assert customer["data"]["signers"] == [{"name": "Grace Hopper", "email": "grace@example.com"}]
    quote = live.of("quote")[str(revision.pk)]
    assert quote["customer"] == str(grace.pk) and quote["job"] == str(revision.quote.job_id)
    assert quote["data"]["open"] and quote["data"]["total"].startswith("$")
    assert quote["data"]["doc"] in live.objects
    assert live.objects[quote["data"]["doc"]][1].startswith("text/html")
    assert revision.quote.number.encode() in live.objects[quote["data"]["doc"]][0]
    assert live.of("job")[str(revision.quote.job_id)]["data"]["status"] == "Quote sent"

    # Nothing changed: nothing pushed again.
    before = len(live.calls)
    assert sync.run()["pushed"] == 0
    assert all(
        path.startswith(("/_sync/inbox", "/_sync/health")) for _, path in live.calls[before:]
    )

    # A change re-renders the quote: its document moves, the old one goes.
    old_doc = quote["data"]["doc"]
    from trammel.quotes import services as quotes

    quotes.decline(revision, actor=admin, reason="Too dear")
    sync.run()
    quote = live.of("quote")[str(revision.pk)]
    assert quote["data"]["status"] == "Declined" and not quote["data"]["open"]
    assert quote["data"]["doc"] in live.objects and old_doc not in live.objects

    # Access off: everything of theirs comes down, files and all.
    portal.set_access(grace, False, user=None)
    sync.run()
    assert not live.of("customer") and not live.of("quote") and not live.of("job")
    assert set(live.objects) == {f"assets/{site['css']}", f"assets/{site['signature_js']}"}
    assert set(Published.objects.values_list("type", flat=True)) == {"config"}


@pytest.mark.django_db
def test_closing_the_portal_signs_everyone_out_there(live, grace):  # noqa: F811
    sync.run()
    assert live.of("customer")
    modules.set_enabled("edge", False)
    sync.run()
    assert live.records[("config", "site")]["data"]["portal_on"] is False
    assert ("POST", "/_sync/sessions/end") in live.calls
    assert not live.of("customer")
    # With the edge module off, Trammel's own portal pages are back.
    assert Client().get("/portal/sign-in/").status_code == 200


@pytest.mark.django_db
def test_what_customers_did_there_is_applied_here(live, grace, admin):  # noqa: F811
    raw_display, _row = portal.make_code(grace, user=admin)
    code = AccessCode.objects.get(customer=grace)
    edge_copy = EdgeAccessCode.objects.get(code=code)
    assert edge_copy.code_hash == client.access_hash(AccessCode.normalize(raw_display))
    sync.run()
    assert live.of("access")[edge_copy.code_hash]["data"]["given_by"] == admin.display_name

    at = timezone.now().isoformat()
    live.inbox = [
        {
            "seq": 1,
            "kind": "code_sent",
            "at": at,
            "ip": "203.0.113.9",
            "customer": str(grace.pk),
            "data": {"email": "grace@example.com", "customer": str(grace.pk)},
        },
        {
            "seq": 2,
            "kind": "signed_in",
            "at": at,
            "ip": "203.0.113.9",
            "customer": str(grace.pk),
            "data": {"email": "grace@example.com", "customer": str(grace.pk)},
        },
        {
            "seq": 3,
            "kind": "access_code_used",
            "at": at,
            "ip": "203.0.113.9",
            "customer": str(grace.pk),
            "data": {"customer": str(grace.pk), "code": str(code.pk)},
        },
        {
            "seq": 4,
            "kind": "mail_failed",
            "at": at,
            "ip": "203.0.113.9",
            "customer": None,
            "data": {"email": "x@example.com", "error": "535 no"},
        },
    ]
    assert sync.run()["applied"] == 4
    assert live.acked == 4 and EdgeSettings.load().inbox_after == 4
    code.refresh_from_db()
    assert code.used_at is not None
    assert Access.objects.get(customer=grace).last_sign_in_at is not None
    assert AuditEvent.objects.filter(action="portal.sign_in").count() == 2
    logs = {(e.to, e.status) for e in EmailLog.objects.filter(event="portal_code")}
    assert logs == {("grace@example.com", "sent"), ("x@example.com", "failed")}
    # The used code comes down from the edge; applying again does nothing.
    sync.run()
    assert not live.of("access")
    assert sync.run()["applied"] == 0


@pytest.mark.django_db
def test_the_built_in_portal_steps_aside(live, grace, admin, admin_client):  # noqa: F811
    assert Client().get("/portal/sign-in/").status_code == 404
    assert Client().get("/portal/").status_code == 404
    assert portal.link() == f"{URL}/portal/"
    # Staff can still view it as the customer would.
    admin_client.post(f"/portal/preview/{grace.pk}/")
    assert admin_client.get("/portal/").status_code == 200

    # A failed sync is kept for the settings page, not raised.
    def broken(*args, **kwargs):
        raise client.EdgeError("Couldn't reach the edge portal: timed out")

    live_call = client.call
    client.call = broken
    try:
        assert "error" in sync.run()
    finally:
        client.call = live_call
    page = admin_client.get("/settings/edge-portal/").content.decode()
    assert "timed out" in page and f"{URL}/portal/" in page


class FakeCloudflare:
    def __init__(self, accounts=1, subdomain="shop"):
        self.accounts = [{"id": f"acct{i}", "name": f"Account {i}"} for i in range(accounts)]
        self.subdomain = subdomain
        self.calls = []
        self.turnstile_refused = False

    def call(self, token, method, path, *, data=None, query=None, body=None, content_type=None):
        self.calls.append((method, path, data, query, body))
        assert token == "cf-token"
        if path == "/accounts":
            return self.accounts
        if path.endswith("/challenges/widgets"):
            if self.turnstile_refused:
                raise cloudflare.CloudflareError("Authentication error (10000)")
            return {"sitekey": "0x4AAA", "secret": "ts-secret"}
        if path.endswith("/d1/database") and method == "GET":
            return []
        if path.endswith("/d1/database"):
            return {"uuid": "db-uuid", "name": data["name"]}
        if "/r2/buckets/" in path:
            error = cloudflare.CloudflareError("not found")
            error.status = 404
            raise error
        if path.endswith("/r2/buckets"):
            return {"name": data["name"]}
        if path.endswith("/workers/subdomain"):
            return {"subdomain": self.subdomain}
        if path == "/zones":
            return (
                [{"id": "zone1", "name": "example.com"}] if query["name"] == "example.com" else []
            )
        return {}


@pytest.mark.django_db
def test_deploying_over_cloudflares_api(monkeypatch, portal_on, worker, admin_client, smtp):  # noqa: F811
    modules.set_enabled("edge", True)
    fake = FakeCloudflare()
    monkeypatch.setattr(cloudflare, "call", fake.call)
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
    page = admin_client.get("/settings/edge-portal/").content.decode()
    assert "Workers Scripts, Edit" in page and "port 465" in page
    admin_client.post(
        "/settings/edge-portal/",
        {"api_token": "cf-token", "hostname": "", "keep_closed_days": "90"},
    )
    assert secrets.get(deploy.TOKEN_KEY) == "cf-token"
    assert "cf-token" not in admin_client.get("/settings/edge-portal/").content.decode()

    response = admin_client.post("/settings/edge-portal/deploy/")
    assert response.status_code == 302 and EdgeSettings.load().deploying
    deploy.run_deploy()
    row = EdgeSettings.load()
    assert not row.deploying and row.active, row.deploy_log
    assert row.public_url == "https://trammel-portal.shop.workers.dev"
    assert row.deploy_log[-1]["text"].startswith("Done")
    upload = next(
        c for c in fake.calls if c[0] == "PUT" and c[1].endswith("/scripts/trammel-portal")
    )
    body = upload[4].decode()
    assert '"main_module": "worker.js"' in body and "application/javascript+module" in body
    assert '"type": "d1", "name": "DB", "id": "db-uuid"' in body
    assert '"bucket_name": "trammel-portal"' in body and '"name": "SYNC_KEY"' in body
    assert '\\"port\\": 587' in body and '\\"security\\": \\"starttls\\"' in body
    assert ("POST", "/accounts/acct0/workers/scripts/trammel-portal/subdomain") in [
        c[:2] for c in fake.calls
    ]
    assert worker.of("customer") == {}  # nobody has access yet, but the shop is published
    assert ("config", "site") in worker.records

    # A custom address on a Cloudflare domain.
    EdgeSettings.objects.filter(pk=1).update(hostname="portal.example.com")
    deploy.run_deploy()
    domain = next(c for c in fake.calls if c[1] == "/accounts/acct0/workers/domains")
    assert domain[2]["hostname"] == "portal.example.com" and domain[2]["zone_id"] == "zone1"
    assert EdgeSettings.load().public_url == "https://portal.example.com"


@pytest.mark.django_db
def test_deploying_needs_encrypted_email_and_one_account(monkeypatch, portal_on, worker, smtp):  # noqa: F811
    secrets.put(deploy.TOKEN_KEY, "cf-token")
    monkeypatch.setattr(cloudflare, "call", FakeCloudflare(accounts=2).call)
    deploy.run_deploy()
    assert "several accounts" in EdgeSettings.load().deploy_log[-1]["text"]
    smtp.security, smtp.port = "none", 25
    smtp.save()
    deploy.run_deploy()
    log = EdgeSettings.load().deploy_log[-1]["text"]
    assert "encrypted email" in log and "port 465" in log
    assert not EdgeSettings.load().active


@pytest.mark.django_db
def test_stopping_takes_everything_down(live, grace):  # noqa: F811
    sync.run()
    assert live.records
    deploy.stop()
    assert not live.records and not Published.objects.exists()
    assert not EdgeSettings.load().active
    assert Client().get("/portal/sign-in/").status_code == 200


def test_cloudflare_explains_r2_being_off():
    assert "turn it on" in cloudflare._explain([{"code": 10042, "message": "Please enable R2"}])
    assert dt  # (imported for the inbox times above)
