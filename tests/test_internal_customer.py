"""Internal shop jobs: work for the shop itself (fixtures, improvements, stock made ahead).

They run through the job lifecycle without a customer to approve a quote or
accept an agreement, and are never invoiced.
"""

import datetime as dt
import re
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from trammel.agreements import services as agreements
from trammel.billing import services as billing
from trammel.customers import services as customers
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.reports import services as reports
from trammel.workorders import services as workorders


@pytest.fixture
def internal_job(setup_done):
    return jobs.create_job(Job(title="Outfeed table", customer=Customer.internal()))


@pytest.mark.django_db
def test_there_is_one_internal_customer_and_it_stays(setup_done):
    shop = Customer.internal()
    assert Customer.internal() == shop and shop.is_internal and shop.name == "Shop (internal)"
    with pytest.raises(IntegrityError), transaction.atomic():
        Customer.objects.create(name="Another shop", is_internal=True)
    with pytest.raises(ValidationError, match="cannot be deleted"):
        customers.delete_customer(shop)


@pytest.mark.django_db
def test_internal_job_is_approved_directly_and_work_starts_without_an_agreement(
    internal_job, admin
):
    assert agreements.satisfied(internal_job)
    labels = [t.label for t in Job.lifecycle.available(internal_job)]
    assert "Approve internal job" in labels
    jobs.transition(internal_job, "approved", actor=admin)
    internal_job.refresh_from_db()
    assert internal_job.status == "approved"
    assert workorders.can_create_for(internal_job) == []
    wo = workorders.create(internal_job, title="Build it")
    assert wo.job == internal_job


@pytest.mark.django_db
def test_customer_jobs_still_need_an_approved_quote(customer, admin):
    job = jobs.create_job(Job(title="Sign", customer=customer))
    assert "Approve internal job" not in [t.label for t in Job.lifecycle.available(job)]
    with pytest.raises(ValidationError):
        jobs.transition(job, "approved", actor=admin)


@pytest.mark.django_db
def test_internal_work_is_never_invoiced(internal_job, admin):
    with pytest.raises(ValidationError, match="not invoiced"):
        billing.create(internal_job.customer, job=internal_job)
    # An internal quote with a deposit approves without making a deposit invoice.
    revision = quotes.create_quote(internal_job)
    form = LINE_FORMS["adhoc"](
        {"description": "Plywood", "quantity": "1", "unit": "each", "unit_price": "80"}
    )
    assert form.is_valid(), form.errors
    quotes.save_line(form.build(revision))
    revision.deposit_kind, revision.deposit_value = "percent", Decimal("50")
    quotes.update_revision(revision, before={})
    quotes.send(revision, actor=admin)
    quotes.approve(revision, actor=admin, approved_by_name="Shop")
    internal_job.refresh_from_db()
    assert internal_job.status == "approved" and not internal_job.invoices.exists()


@pytest.mark.django_db
def test_invoice_screens_refuse_internal_work(admin_client, internal_job):
    page = admin_client.get(f"/jobs/{internal_job.pk}/?tab=invoices").content.decode()
    assert "Internal shop work is not invoiced." in page
    response = admin_client.post(
        "/invoices/new/", {"job": internal_job.pk, "kind": "final"}, follow=True
    )
    assert "Internal shop work is not invoiced." in response.content.decode()
    assert not internal_job.invoices.exists()


@pytest.mark.django_db
def test_internal_job_closes_out_and_stays_out_of_profitability(internal_job, admin):
    for target in ("approved", "in_progress", "completed", "closed"):
        jobs.transition(internal_job, target, actor=admin)
    today = dt.date.today()
    rows, _totals = reports.profitability(today - dt.timedelta(days=1), today)
    assert internal_job not in [r["job"] for r in rows]
    assert reports.job_costing(internal_job).invoiced == 0


@pytest.mark.django_db
def test_job_form_marks_a_job_internal(admin_client, customer):
    page = admin_client.get("/jobs/new/").content.decode()
    assert "Internal shop job" in page
    response = admin_client.post(
        "/jobs/new/", {"title": "Dust collection", "customer": customer.pk, "internal": "on"}
    )
    assert response.status_code == 302
    job = Job.objects.get(title="Dust collection")
    assert job.customer.is_internal
    # The internal customer is never offered in the customer list.
    page = admin_client.get(f"/jobs/{job.pk}/edit/").content.decode()
    assert f'<option value="{job.customer.pk}"' not in page
    assert re.search(r'<input type="checkbox" name="internal"[^>]* checked', page)
    admin_client.post(
        f"/jobs/{job.pk}/edit/", {"title": "Dust collection", "customer": customer.pk}
    )
    job.refresh_from_db()
    assert job.customer == customer
