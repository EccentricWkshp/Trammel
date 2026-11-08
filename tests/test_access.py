"""SPEC §2: per-action and per-record permission checks, from the start."""

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404

from tests.conftest import make_user
from tests.testapp.models import Widget
from trammel.accounts.models import ApiKey
from trammel.core import access, modules
from trammel.core.access import SYSTEM, Actor, UnknownPermission
from trammel.customers.models import Customer


def customer_actor(customer):
    return Actor(kind="customer", customer_id=customer.pk, label=str(customer))


@pytest.mark.django_db
def test_admin_holds_everything_and_roles_grant_their_union(admin, setup_done):
    assert access.has_perm(admin, "testapp.force")
    user = make_user("two@example.com", "customers.view")
    from trammel.accounts.models import Role

    user.roles.add(Role.objects.create(name="Extra", permissions=["testapp.edit"]))
    assert access.permissions_of(access.as_actor(user)) == {"customers.view", "testapp.edit"}
    assert not access.has_perm(user, "customers.delete")


@pytest.mark.django_db
def test_inactive_and_anonymous_hold_nothing(setup_done):
    user = make_user("gone@example.com", "customers.view")
    user.is_active = False
    assert not access.has_perm(user, "customers.view")
    assert not access.has_perm(None, "customers.view")


@pytest.mark.django_db
def test_unknown_permission_is_a_programming_error(admin):
    with pytest.raises(UnknownPermission):
        access.has_perm(admin, "customers.veiw")
    with pytest.raises(UnknownPermission):
        admin.has_perm("customers.veiw")


@pytest.mark.django_db
def test_disabled_module_is_read_only(admin):
    modules.set_enabled("testapp", False)
    assert access.has_perm(admin, "testapp.view")
    assert not access.has_perm(admin, "testapp.edit")
    assert not access.has_perm(SYSTEM, "testapp.edit")
    modules.set_enabled("testapp", True)
    assert access.has_perm(admin, "testapp.edit")


@pytest.mark.django_db
def test_customer_principal_sees_only_its_own_records(setup_done):
    mine = Customer.objects.create(name="Mine")
    theirs = Customer.objects.create(name="Theirs")
    w_mine = Widget.objects.create(name="a", customer=mine)
    w_theirs = Widget.objects.create(name="b", customer=theirs)
    actor = customer_actor(mine)

    assert list(access.scoped(actor, "testapp.view", Widget.objects.all())) == [w_mine]
    assert list(access.scoped(actor, "customers.view", Customer.objects.all())) == [mine]
    assert access.has_perm(actor, "testapp.view", w_mine)
    assert not access.has_perm(actor, "testapp.view", w_theirs)
    # Never a staff permission, and a foreign record is 404, not 403.
    assert not access.has_perm(actor, "testapp.edit", w_mine)
    with pytest.raises(Http404):
        access.get_or_404(actor, "testapp.view", Widget.objects.all(), pk=w_theirs.pk)
    with pytest.raises(Http404):
        access.get_or_404(actor, "testapp.edit", Widget.objects.all(), pk=w_mine.pk)


@pytest.mark.django_db
def test_staff_without_permission_get_403_not_404(staff, customer):
    with pytest.raises(PermissionDenied):
        access.get_or_404(staff, "customers.edit", Customer.objects.all(), pk=customer.pk)
    assert (
        access.get_or_404(staff, "customers.view", Customer.objects.all(), pk=customer.pk)
        == customer
    )
    with pytest.raises(Http404):
        access.get_or_404(staff, "customers.view", Customer.objects.all(), pk="not-a-uuid")


@pytest.mark.django_db
def test_api_key_scope_is_intersected_with_its_user(setup_done):
    user = make_user("keys@example.com", "customers.view", "customers.create", "accounts.api_keys")
    key, token = ApiKey.issue(user, "printer", ["customers.view"])
    actor = ApiKey.authenticate(token).actor()
    assert access.has_perm(actor, "customers.view")
    assert not access.has_perm(actor, "customers.create")
    assert not access.is_admin(actor)

    # Losing the permission from the role removes it from the key too.
    user.roles.update(permissions=["accounts.api_keys"])
    actor = ApiKey.authenticate(token).actor()
    assert not access.has_perm(actor, "customers.view")


@pytest.mark.django_db
def test_api_key_cannot_be_issued_beyond_the_user(staff):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        ApiKey.issue(staff, "too much", ["customers.delete"])
