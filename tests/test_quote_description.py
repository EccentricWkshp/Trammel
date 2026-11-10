"""A narrative description on quotes and change orders: what is being done, in
words, beyond the list of lines."""

import json
from decimal import Decimal

import pytest
from django.test import Client

from trammel.accounts.models import ApiKey
from trammel.core import access
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.workorders import services as wos

TEXT = "A walnut dining table seating six.\nBreadboard top, tapered legs, oil finish."


def _line(revision, price="1200"):
    form = LINE_FORMS["adhoc"](
        {"description": "Table", "quantity": "1", "unit": "each", "unit_price": price}
    )
    assert form.is_valid(), form.errors
    quotes.save_line(form.build(revision))


@pytest.fixture
def revision(setup_done, customer):
    job = jobs.create_job(Job(title="Dining table", customer=customer, description="Walnut"))
    revision = quotes.create_quote(job)
    _line(revision)
    return revision


@pytest.mark.django_db
def test_the_box_above_the_lines_saves_it_and_the_document_shows_it(admin_client, revision):
    page = admin_client.get(f"/quotes/r/{revision.pk}/").content.decode()
    assert 'name="description"' in page
    admin_client.post(f"/quotes/r/{revision.pk}/description/", {"description": f"  {TEXT}  "})
    revision.refresh_from_db()
    assert revision.description == TEXT
    document = admin_client.get(f"/quotes/r/{revision.pk}/document/").content.decode()
    assert 'class="description">A walnut dining table' in document
    assert document.index("Breadboard top") < document.index("<th>Description</th>")


@pytest.mark.django_db
def test_fixed_once_sent_and_carried_to_the_next_revision(admin_client, admin, revision):
    revision.description = TEXT
    revision.save()
    quotes.send(revision, actor=admin)
    page = admin_client.get(f"/quotes/r/{revision.pk}/").content.decode()
    assert 'name="description"' not in page and "Breadboard top" in page
    admin_client.post(f"/quotes/r/{revision.pk}/description/", {"description": "Changed"})
    revision.refresh_from_db()
    assert revision.description == TEXT
    assert quotes.revise(revision, actor=admin).description == TEXT


@pytest.mark.django_db
def test_the_api_reads_and_sets_it(admin, revision):
    scopes = sorted(access.permissions_of(access.as_actor(admin)))
    issued = ApiKey.issue(admin, "t", scopes)
    token = issued[1] if isinstance(issued, tuple) else issued
    client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = client.patch(
        f"/api/v1/quotes/{revision.pk}",
        data=json.dumps({"description": TEXT}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert response.json()["description"] == TEXT


@pytest.mark.django_db
def test_the_shop_sees_it_on_the_work_order(admin, revision):
    revision.description = TEXT
    revision.save()
    quotes.send(revision, actor=admin)
    quotes.approve(revision, actor=admin, approved_by_name="Grace", accept_agreement=True)
    job = revision.quote.job
    wo = wos.create(job, revision=revision)
    assert wo.instructions == f"Walnut\n\n{TEXT}"

    change = quotes.create_quote(job, title="Add a drawer")
    _line(change, price="150")
    change.description = "A shallow drawer on the left, with soft-close slides."
    change.save()
    quotes.send(change, actor=admin)
    quotes.approve(change, actor=admin, approved_by_name="Grace")
    op = wo.operations.get(source_revision=change)
    assert op.description == change.description
    assert Decimal(change.quote.revisions.get().total) == Decimal("150.00")
