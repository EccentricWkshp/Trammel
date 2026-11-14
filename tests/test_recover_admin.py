"""Getting back in when no admin can sign in: manage.py recover_admin (FR-ADM-5)."""

import re
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from tests.conftest import make_user
from trammel.accounts import security
from trammel.accounts.models import LoginAttempt
from trammel.core.models import AuditEvent


def _run(*args) -> str:
    out = StringIO()
    call_command("recover_admin", *args, stdout=out)
    return out.getvalue()


def _link(output: str) -> str:
    return re.search(r"https?://\S+/accounts/set-password/\S+/", output).group(0)


@pytest.mark.django_db
def test_without_an_address_it_lists_the_admins(admin):
    make_user("sam@example.com", "customers.view")
    output = _run()
    assert "admin@example.com" in output and "Ada Admin" in output
    assert "active" in output and "no two-factor" in output and "never signed in" in output
    # Only admins: a staff account isn't one to recover.
    assert "sam@example.com" not in output


@pytest.mark.django_db
def test_an_admin_gets_a_link_that_sets_a_new_password(client, admin):
    output = _run("ADMIN@Example.com")
    path = _link(output).split("://", 1)[1].split("/", 1)[1]
    response = client.get("/" + path)
    assert response.status_code == 200
    response = client.post(
        "/" + path,
        {"new_password1": "a fresh long passphrase", "new_password2": "a fresh long passphrase"},
    )
    assert response.status_code == 302
    admin.refresh_from_db()
    assert admin.check_password("a fresh long passphrase")
    # It works once.
    assert client.get("/" + path).status_code == 400
    issued = AuditEvent.objects.get(action="user.password_link")
    assert issued.actor_kind == "system" and issued.entity_label == str(admin)


@pytest.mark.django_db
def test_an_unknown_address_or_a_staff_account_is_refused(admin):
    with pytest.raises(CommandError, match="without an address to list"):
        _run("andy@example.com")
    make_user("sam@example.com", "customers.view")
    with pytest.raises(CommandError, match="isn't an admin"):
        _run("sam@example.com")
    assert not AuditEvent.objects.filter(action="user.password_link").exists()


@pytest.mark.django_db
def test_a_deactivated_admin_is_reactivated(admin):
    admin.is_active = False
    admin.deactivated_at = timezone.now()
    admin.save()
    output = _run("admin@example.com")
    admin.refresh_from_db()
    assert admin.is_active and admin.deactivated_at is None
    assert "Reactivated" in output and _link(output)
    assert AuditEvent.objects.filter(action="user.activate").exists()


@pytest.mark.django_db
def test_two_factor_stays_unless_asked_to_turn_it_off(admin):
    admin.totp_secret = "JBSWY3DPEHPK3PXP"
    admin.totp_confirmed_at = timezone.now()
    admin.recovery_codes = ["x"]
    admin.save()
    assert "Two-factor sign-in stays on" in _run("admin@example.com")
    admin.refresh_from_db()
    assert admin.totp_confirmed_at is not None
    output = _run("admin@example.com", "--reset-mfa")
    admin.refresh_from_db()
    assert admin.totp_confirmed_at is None and admin.totp_secret == ""
    assert admin.recovery_codes == []
    assert "Turned off two-factor" in output
    assert AuditEvent.objects.get(action="auth.mfa_reset").actor_kind == "system"


@pytest.mark.django_db
def test_failed_sign_ins_are_shown_and_cleared(admin, settings):
    # Someone trying addresses that aren't the admin's, from one machine,
    # until that machine is refused for every account.
    for _ in range(settings.LOGIN_MAX_FAILURES_PER_IP):
        security.record_attempt("andy@example.com", "127.0.0.1", succeeded=False)
    for _ in range(3):
        security.record_attempt("admin@example.com", "127.0.0.1", succeeded=False)
    assert security.is_locked("admin@example.com", "127.0.0.1")

    output = _run()
    assert "3 failed sign-ins" in output and "127.0.0.1" in output and "--clear-failures" in output

    # Recovering the admin forgets their own failures, and says the network
    # is still refused.
    output = _run("admin@example.com")
    assert "Forgot 3 recent failed sign-ins" in output and "still refused" in output
    assert security.ip_locked("127.0.0.1")

    output = _run("--clear-failures")
    assert f"Forgot {settings.LOGIN_MAX_FAILURES_PER_IP} recent failed sign-ins" in output
    assert not security.is_locked("admin@example.com", "127.0.0.1")
    assert not LoginAttempt.objects.filter(succeeded=False).exists()
    assert AuditEvent.objects.get(action="auth.failures_cleared").actor_kind == "system"
