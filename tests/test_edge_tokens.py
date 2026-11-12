"""Account API tokens (0.9.3): the account ID given by hand, and a bot check
made in Cloudflare's dashboard, since such a token can't list accounts or
manage Turnstile."""

import pytest

from tests.test_edge import FakeCloudflare, worker  # noqa: F401
from tests.test_portal import portal_on, smtp  # noqa: F401
from trammel.core import modules, secrets
from trammel.edge import cloudflare, deploy
from trammel.edge.models import EdgeSettings

ACCOUNT = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def fake(monkeypatch):
    fake = FakeCloudflare()
    fake.accounts = [{"id": ACCOUNT, "name": "The Shop"}]
    fake.account_token = True
    monkeypatch.setattr(cloudflare, "call", fake.call)
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
    return fake


def _settings(client, **fields):
    return client.post(
        "/settings/edge-portal/",
        {
            "api_token": "cf-token",
            "account_id": "",
            "hostname": "",
            "keep_closed_days": "90",
            "turnstile_own_sitekey": "",
            "turnstile_secret": "",
            **fields,
        },
    )


@pytest.mark.django_db
def test_an_account_token_with_the_account_id_and_a_bot_check_made_by_hand(
    fake,
    portal_on,  # noqa: F811
    worker,  # noqa: F811
    smtp,  # noqa: F811
    admin_client,
):
    modules.set_enabled("edge", True)
    page = admin_client.get("/settings/edge-portal/").content.decode()
    assert "Account API Tokens" in page and "payment method" in page

    # Without the ID, an account token can't find its account: the deploy asks.
    _settings(admin_client)
    deploy.run_deploy()
    assert "Enter the Cloudflare account ID" in EdgeSettings.load().deploy_log[-1]["text"]

    # A site key needs its secret.
    response = _settings(admin_client, account_id=ACCOUNT, turnstile_own_sitekey="0x4OWN")
    assert response.status_code == 200 and "Paste the widget" in response.content.decode()
    response = _settings(
        admin_client,
        account_id=ACCOUNT.upper(),
        turnstile_own_sitekey="0x4OWN",
        turnstile_secret="own-secret",
    )
    assert response.status_code == 302
    assert secrets.get(deploy.TURNSTILE_OWN_KEY) == "own-secret"
    assert "own-secret" not in admin_client.get("/settings/edge-portal/").content.decode()

    deploy.run_deploy()
    row = EdgeSettings.load()
    assert row.active, row.deploy_log
    assert row.account_id == ACCOUNT and row.account_name == "The Shop"
    assert row.turnstile_sitekey == "0x4OWN"
    assert not any(c[1].endswith("/challenges/widgets") for c in fake.calls)
    upload = next(
        c for c in fake.calls if c[0] == "PUT" and c[1].endswith("/scripts/trammel-portal")
    )
    assert '"name": "TURNSTILE_SECRET", "text": "own-secret"' in upload[4].decode()
    assert worker.records[("config", "site")]["data"]["turnstile"] == "0x4OWN"

    # Removing it leaves the widget made by hand alone.
    deploy.remove()
    deleted = [c[1] for c in fake.calls if c[0] == "DELETE"]
    assert deleted and not any("/challenges/widgets" in path for path in deleted)
    assert secrets.get(deploy.TURNSTILE_OWN_KEY) == "own-secret"

    # Clearing the site key forgets its secret.
    _settings(admin_client, account_id=ACCOUNT)
    assert not secrets.is_set(deploy.TURNSTILE_OWN_KEY)


@pytest.mark.django_db
def test_an_account_token_without_turnstile_goes_ahead_and_says_how(
    fake,
    portal_on,  # noqa: F811
    worker,  # noqa: F811
    smtp,  # noqa: F811
):
    modules.set_enabled("edge", True)
    secrets.put(deploy.TOKEN_KEY, "cf-token")
    EdgeSettings.load()
    EdgeSettings.objects.filter(pk=1).update(account_id=ACCOUNT)
    deploy.run_deploy()
    row = EdgeSettings.load()
    assert row.active and not row.turnstile_sitekey
    assert any("paste its keys on this page" in step["text"] for step in row.deploy_log)


@pytest.mark.django_db
def test_a_wrong_account_id(fake, portal_on, worker, smtp, admin_client):  # noqa: F811
    response = _settings(admin_client, account_id="acct-1")
    assert response.status_code == 200 and "32 letters and digits" in response.content.decode()
    secrets.put(deploy.TOKEN_KEY, "cf-token")
    EdgeSettings.load()
    EdgeSettings.objects.filter(pk=1).update(account_id="f" * 32)
    deploy.run_deploy()
    assert "can't reach the account whose ID" in EdgeSettings.load().deploy_log[-1]["text"]
