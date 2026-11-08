import pytest
from django.utils import timezone

from trammel.accounts.models import Role, User
from trammel.core.models import BusinessProfile, SystemSettings
from trammel.customers.models import Customer


@pytest.fixture
def setup_done(db):
    system = SystemSettings.load()
    system.setup_completed_at = timezone.now()
    system.save()
    business = BusinessProfile.load()
    business.name = "Test Shop"
    business.save()
    return business


@pytest.fixture
def admin(setup_done):
    return User.objects.create_admin(
        "admin@example.com", "correct horse battery", full_name="Ada Admin"
    )


def make_user(email, *perms, name="Sam Staff"):
    user = User.objects.create_user(email, "correct horse battery", full_name=name)
    if perms:
        role = Role.objects.create(name=f"role-{email}", permissions=list(perms))
        user.roles.add(role)
    return user


@pytest.fixture
def staff(setup_done):
    """A user who can view and create customers, but not edit or delete them."""
    return make_user("staff@example.com", "customers.view", "customers.create")


@pytest.fixture
def admin_client(client, admin):
    client.force_login(admin)
    return client


@pytest.fixture
def staff_client(client, staff):
    client.force_login(staff)
    return client


@pytest.fixture
def no_job_gates(monkeypatch):
    """Remove the checks other modules register on job transitions (quotes, agreements…)."""
    from collections import defaultdict

    from trammel.jobs import hooks

    monkeypatch.setattr(hooks, "_checks", defaultdict(list))


@pytest.fixture
def customer(setup_done):
    return Customer.objects.create(name="Grace Hopper", email="grace@example.com", phone="555-0100")
