"""SPEC §9: TOTP second factor, recovery codes, MFA required per role, magic links."""

import re

import pytest
from django.core import mail

from tests.conftest import make_user
from trammel.accounts import mfa
from trammel.accounts.models import MagicLink, Role
from trammel.core import tasks
from trammel.notifications.models import SmtpSettings

PASSWORD = "correct horse battery"


def test_rfc6238_vector():
    # RFC 6238 appendix B, SHA-1, T = 59 s: 94287082 → last 6 digits.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of "12345678901234567890"
    assert mfa.code_for(secret, at=59) == "287082"
    assert mfa.matching_step(secret, "287082", at=59 + 30) is not None  # one step of drift
    assert mfa.matching_step(secret, "287082", at=59 + 120) is None


@pytest.fixture
def enrolled(admin):
    secret = mfa.new_secret()
    codes = mfa.enable(admin, secret)
    admin.refresh_from_db()
    return admin, secret, codes


@pytest.mark.django_db
def test_sign_in_asks_for_the_second_factor(client, enrolled):
    admin, secret, _codes = enrolled
    response = client.post("/accounts/login/", {"email": admin.email, "password": PASSWORD})
    assert response.url == "/accounts/two-factor/"
    assert "_auth_user_id" not in client.session
    wrong = client.post("/accounts/two-factor/", {"code": "000000"})
    assert "not right" in wrong.content.decode()
    import time

    code = mfa.code_for(secret, at=time.time() + 30)  # the next step, never used before
    assert client.post("/accounts/two-factor/", {"code": code}).status_code == 302
    assert client.session["_auth_user_id"] == str(admin.pk)


@pytest.mark.django_db
def test_codes_are_single_use(enrolled):
    import time

    admin, secret, codes = enrolled
    code = mfa.code_for(secret, at=time.time() + 30)
    assert mfa.verify(admin, code)
    assert not mfa.verify(admin, code)  # replayed
    assert mfa.verify(admin, codes[0])
    assert not mfa.verify(admin, codes[0])
    admin.refresh_from_db()
    assert len(admin.recovery_codes) == 9


@pytest.mark.django_db
def test_secret_is_encrypted(enrolled):
    admin, secret, _ = enrolled
    assert secret not in admin.totp_secret and mfa.decrypt(admin.totp_secret) == secret


@pytest.mark.django_db
def test_role_can_require_mfa(client, setup_done):
    user = make_user("hand@example.com", "customers.view")
    Role.objects.filter(users=user).update(require_mfa=True)
    client.force_login(user)
    assert client.get("/customers/").url == "/accounts/two-factor/setup/"
    page = client.get("/accounts/two-factor/setup/")
    assert page.status_code == 200 and "<svg" in page.content.decode()
    secret = client.session["mfa_new_secret"]
    response = client.post("/accounts/two-factor/setup/", {"code": mfa.code_for(secret)})
    assert "recovery codes" in response.content.decode()
    assert client.get("/customers/").status_code == 200


@pytest.mark.django_db
def test_admin_resets_mfa(admin_client, setup_done):
    user = make_user("lost@example.com")
    mfa.enable(user, mfa.new_secret())
    admin_client.post(f"/accounts/users/{user.pk}/reset-mfa/")
    user.refresh_from_db()
    assert not mfa.enabled(user)


@pytest.mark.django_db
def test_magic_link(client, admin, django_capture_on_commit_callbacks):
    smtp = SmtpSettings.load()
    smtp.host, smtp.from_address = "smtp.example.com", "shop@example.com"
    smtp.save()
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/accounts/email-link/", {"email": admin.email})
        client.post("/accounts/email-link/", {"email": "nobody@example.com"})
    tasks.run_pending()
    assert len(mail.outbox) == 1
    link = re.search(r"https?://\S+/accounts/email-link/\S+/", mail.outbox[0].body).group(0)
    path = link.split("8000", 1)[-1] if "8000" in link else "/" + link.split("/", 3)[3]
    # Opening the link only offers a button; a mail scanner opening it uses nothing.
    assert "Sign in" in client.get(path).content.decode()
    assert "_auth_user_id" not in client.session
    assert client.post(path).status_code == 302
    assert client.session["_auth_user_id"] == str(admin.pk)
    client.logout()
    assert client.get(path).status_code == 400  # used up
    assert client.post(path).status_code == 400
    assert MagicLink.objects.get().used_at is not None


@pytest.mark.django_db
def test_magic_link_still_needs_the_second_factor(client, enrolled):
    admin, _secret, _codes = enrolled
    token = MagicLink.issue(admin)
    response = client.post(f"/accounts/email-link/{token}/")
    assert response.url == "/accounts/two-factor/" and "_auth_user_id" not in client.session
