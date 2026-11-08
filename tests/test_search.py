"""FR-SRCH-1: one search across modules, scoped by permission, on PostgreSQL."""

import pytest

from tests.conftest import make_user
from trammel.core import search
from trammel.jobs import services as jobs
from trammel.jobs.models import Job


@pytest.mark.django_db
def test_search_finds_by_number_name_phone_and_text(admin, customer):
    job = jobs.create_job(
        Job(title="Walnut sign", customer=customer, description="Engraved lettering, routed edges")
    )
    kinds = lambda term: {k for k, _ in search.run(admin, term)}  # noqa: E731
    assert kinds(job.number) == {"Jobs"}
    assert "Customers" in kinds("0100")  # the end of a phone number
    assert "Jobs" in kinds("engraving")  # full-text: stemmed, not a substring
    assert search.run(admin, "x") == []


@pytest.mark.django_db
def test_search_respects_permissions(setup_done, customer):
    jobs.create_job(Job(title="Secret project", customer=customer))
    viewer = make_user("viewer@example.com", "customers.view")
    assert {k for k, _ in search.run(viewer, "Secret")} == set()
    assert {k for k, _ in search.run(viewer, "Grace")} == {"Customers"}


@pytest.mark.django_db
def test_search_page(admin_client, customer):
    page = admin_client.get("/search/?q=grace").content.decode()
    assert "Grace Hopper" in page
