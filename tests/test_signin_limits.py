"""Sign-in limits that a correct password can't reset, and second factors used
once even by requests at the same moment (SEC-04, SEC-05, 0.10.5)."""

import pytest
from django.test import Client
from django.utils import timezone

from trammel.accounts import mfa, security
from trammel.accounts.models import User


def _wrong(secret):
    return next(
        str(n).zfill(6) for n in range(1000) if mfa.matching_step(secret, str(n).zfill(6)) is None
    )


@pytest.mark.django_db
def test_the_password_alone_never_clears_second_factor_failures(admin):
    secret = mfa.new_secret()
    mfa.enable(admin, secret)
    wrong = _wrong(secret)
    refused = False
    for n in range(60):
        # A new address each time, all in one IPv6 /64.
        client = Client(REMOTE_ADDR=f"2001:db8::{n + 1:x}")
        response = client.post(
            "/accounts/login/", {"email": admin.email, "password": "correct horse battery"}
        )
        if response.status_code != 302:
            refused = "Too many" in response.content.decode()
            break
        client.post("/accounts/two-factor/", {"code": wrong})
    assert refused and n <= 10
    assert security.is_locked(admin.email, "2001:db8::ffff")
    # Another network isn't locked out by it, short of the account's own cap.
    assert not security.is_locked(admin.email, "2001:db9::1")


@pytest.mark.django_db
def test_five_wrong_codes_start_sign_in_again(admin):
    secret = mfa.new_secret()
    mfa.enable(admin, secret)
    wrong = _wrong(secret)
    client = Client(REMOTE_ADDR="203.0.113.9")
    client.post("/accounts/login/", {"email": admin.email, "password": "correct horse battery"})
    for _ in range(4):
        assert client.post("/accounts/two-factor/", {"code": wrong}).status_code == 200
    response = client.post("/accounts/two-factor/", {"code": wrong})
    assert response.status_code == 302 and response["Location"] == "/accounts/login/"
    # The challenge is gone: a right code now needs the password again.
    code = mfa.code_for(secret, at=timezone.now().timestamp() + 30)
    assert client.post("/accounts/two-factor/", {"code": code})["Location"] == "/accounts/login/"


@pytest.mark.django_db
@pytest.mark.parametrize("recovery", [False, True])
def test_a_second_factor_is_used_once_by_requests_at_once(admin, recovery):
    secret = mfa.new_secret()
    codes = mfa.enable(admin, secret)
    first, second = User.objects.get(pk=admin.pk), User.objects.get(pk=admin.pk)
    code = codes[0] if recovery else mfa.code_for(secret, at=timezone.now().timestamp() + 30)
    assert mfa.verify(first, code)
    assert not mfa.verify(second, code)


def test_ipv6_counts_by_its_64():
    assert security.network("2001:db8::1") == security.network("2001:db8::ffff") == "2001:db8::"
    assert security.network("203.0.113.9") == "203.0.113.9"
    assert security.network("::ffff:203.0.113.9") == "203.0.113.9"
    assert security.network("not an address") is None


@pytest.mark.django_db
def test_public_sign_in_requests_are_limited_before_they_queue(admin, monkeypatch):
    """SEC-07 (0.10.6): the pages answer the same, but only so much is queued."""
    from trammel.core import tasks
    from trammel.core.models import BackgroundTask
    from trammel.notifications.models import SmtpSettings

    smtp = SmtpSettings.load()
    smtp.host, smtp.from_address = "smtp.example.com", "shop@example.com"
    smtp.save()
    client = Client(REMOTE_ADDR="198.51.100.10")
    for _ in range(30):
        response = client.post("/accounts/email-link/", {"email": "nobody@example.com"})
        assert response.status_code == 302
    magic = BackgroundTask.objects.filter(name="accounts.magic_link")
    assert magic.count() == 1  # one waits for an address at a time

    for n in range(30):
        client.post("/accounts/email-link/", {"email": f"guess{n}@example.com"})
    assert magic.count() == tasks.PUBLIC_PER_NETWORK_HOUR  # from one network, an hour

    monkeypatch.setattr(tasks, "PUBLIC_PENDING_CAP", 25)
    for n in range(10):
        Client(REMOTE_ADDR=f"203.0.113.{n + 1}").post(
            "/accounts/email-link/", {"email": f"far{n}@example.com"}
        )
    assert magic.count() == 25  # and only so many wait in all
