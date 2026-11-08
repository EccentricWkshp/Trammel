"""Sign-in, lockout, users and roles (SPEC §9, FR-ADM-5, FR-ADM-6)."""

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings

from tests.conftest import make_user
from trammel.accounts import services
from trammel.accounts.models import Role, User
from trammel.core.models import AuditEvent


@pytest.mark.django_db
def test_login_and_failed_login_are_audited(client, admin):
    bad = client.post("/accounts/login/", {"email": "admin@example.com", "password": "nope"})
    assert bad.status_code == 200 and "do not match" in bad.content.decode()
    good = client.post(
        "/accounts/login/", {"email": "ADMIN@example.com", "password": "correct horse battery"}
    )
    assert good.status_code == 302
    assert list(AuditEvent.objects.order_by("at").values_list("action", flat=True)) == [
        "auth.login_failed",
        "auth.login",
    ]


@pytest.mark.django_db
def test_login_redirect_refuses_other_hosts(client, admin):
    response = client.post(
        "/accounts/login/?next=https://evil.example/",
        {"email": "admin@example.com", "password": "correct horse battery"},
    )
    assert response.url == "/"


@pytest.mark.django_db
@override_settings(LOGIN_MAX_FAILURES=3)
def test_lockout_after_repeated_failures(client, admin):
    for _ in range(3):
        client.post("/accounts/login/", {"email": "admin@example.com", "password": "wrong"})
    # The right password is refused while locked, without being checked.
    response = client.post(
        "/accounts/login/", {"email": "admin@example.com", "password": "correct horse battery"}
    )
    assert "Too many" in response.content.decode()
    assert "_auth_user_id" not in client.session
    assert AuditEvent.objects.filter(action="auth.locked").exists()


@pytest.mark.django_db
def test_logout_requires_post(admin_client):
    assert admin_client.get("/accounts/logout/").status_code == 405
    assert admin_client.post("/accounts/logout/").status_code == 302


@pytest.mark.django_db
def test_admin_creates_user_and_gets_password_link(admin_client, client):
    role = Role.objects.create(name="Shop hand", permissions=["customers.view"])
    response = admin_client.post(
        "/accounts/users/new/",
        {"full_name": "Pat Hand", "email": "pat@example.com", "roles": [role.pk], "timezone": ""},
    )
    assert response.status_code == 200
    link = response.context["link"]
    user = User.objects.get(email="pat@example.com")
    assert not user.has_usable_password() and list(user.roles.all()) == [role]

    path = link.split("8000", 1)[-1] if "8000" in link else link
    client.logout()
    form = client.get(path)
    assert form.status_code == 200
    done = client.post(
        path,
        {"new_password1": "a sturdy new passphrase", "new_password2": "a sturdy new passphrase"},
    )
    assert done.status_code == 302
    user.refresh_from_db()
    assert user.check_password("a sturdy new passphrase")
    # The link works once.
    assert client.get(path).status_code == 400


@pytest.mark.django_db
def test_last_admin_cannot_be_removed_or_deactivated(admin):
    other = make_user("other@example.com")
    with pytest.raises(ValidationError):
        services.set_active(admin, False, by=other)
    before = {"is_admin": True}
    admin.is_admin = False
    with pytest.raises(ValidationError):
        services.save_user(admin, before=before)


@pytest.mark.django_db
def test_cannot_deactivate_yourself(admin):
    User.objects.create_admin("second@example.com", "x" * 12, full_name="Second")
    with pytest.raises(ValidationError, match="your own"):
        services.set_active(admin, False, by=admin)


@pytest.mark.django_db
def test_deactivated_user_is_signed_out_and_keeps_records(client, admin):
    user = make_user("leaver@example.com", "customers.view")
    client.force_login(user)
    assert client.get("/customers/").status_code == 200
    services.set_active(user, False, by=admin)
    assert client.get("/customers/").status_code == 302  # back to sign-in
    assert User.objects.filter(pk=user.pk).exists()


@pytest.mark.django_db
def test_role_rejects_unknown_permissions(setup_done):
    role = Role(name="Typo", permissions=["customers.veiw"])
    with pytest.raises(ValidationError):
        role.full_clean()


@pytest.mark.django_db
def test_user_admin_is_admin_only(staff_client):
    assert staff_client.get("/accounts/users/").status_code == 403
    assert staff_client.get("/accounts/roles/").status_code == 403
