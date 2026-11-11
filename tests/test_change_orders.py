"""Change orders: quotes of their own kind for anything priced after a job is
approved (FR-WO-5, FR-AGR-6), and deleting quotes nothing has happened to."""

from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from tests.conftest import make_user
from trammel.billing import services as billing
from trammel.billing.models import Invoice
from trammel.catalog.models import Product
from trammel.core.models import ShopRates
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.quotes.models import Quote, QuoteRevision
from trammel.reports import services as reports
from trammel.shop.models import Machine, Material
from trammel.workorders import services as wos

D = Decimal


def _line(revision, kind, data, credit=False):
    form = LINE_FORMS[kind](data)
    if credit:
        form.allow_credit()
    assert form.is_valid(), form.errors
    return quotes.save_line(form.build(revision))


def _approve(revision, admin, **kwargs):
    quotes.send(revision, actor=admin)
    quotes.approve(revision, actor=admin, approved_by_name="Grace", user=admin, **kwargs)
    revision.refresh_from_db()
    return revision


@pytest.fixture
def job(setup_done, customer, admin):
    """An approved job: its quote, for a table, approved with the agreement."""
    rates = ShopRates.load()
    rates.labor_hourly_rate = D("60")
    rates.minimum_job_charge = D("500")
    rates.save()
    job = jobs.create_job(Job(title="Dining table", customer=customer))
    revision = quotes.create_quote(job)
    _line(
        revision,
        "adhoc",
        {"description": "Table", "quantity": "1", "unit": "each", "unit_price": "1200"},
    )
    _approve(revision, admin, accept_agreement=True)
    job.refresh_from_db()
    return job


@pytest.fixture
def drawer(job):
    """A draft change order on the job: a drawer."""
    revision = quotes.create_quote(job, title="Add a drawer")
    _line(
        revision,
        "adhoc",
        {"description": "Drawer", "quantity": "1", "unit": "each", "unit_price": "150"},
    )
    return revision


@pytest.mark.django_db
def test_priced_before_approval_is_a_quote_after_is_a_change_order(job, drawer, customer):
    first = job.quotes.get(kind=Quote.Kind.QUOTE)
    assert first.number.startswith("Q-")
    quote = drawer.quote
    assert quote.is_change_order and quote.number.startswith("CO-")
    assert drawer.minimum_charge == 0 and drawer.minimum_adjustment == 0
    assert drawer.total == D("150.00")  # no minimum job charge on a change order

    fresh = jobs.create_job(Job(title="Shelf", customer=customer))
    assert not quotes.create_quote(fresh).quote.is_change_order


@pytest.mark.django_db
def test_the_api_makes_a_change_order_on_an_approved_job(admin, job):
    from django.test import Client

    from trammel.accounts.models import ApiKey

    issued = ApiKey.issue(admin, "t", ["quotes.view", "quotes.create", "jobs.view"])
    token = issued[1] if isinstance(issued, tuple) else issued
    client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = client.post(
        "/api/v1/quotes/",
        {"job": str(job.pk), "title": "Extra leaf"},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert body["kind"] == "change_order" and body["quote_number"].startswith("CO-")


@pytest.mark.django_db
def test_a_change_order_has_quote_detail_and_may_credit(job, drawer):
    laser = Machine.objects.create(name="Laser", kind="laser", hourly_rate=D("90"))
    _line(drawer, "machine", {"machine": str(laser.pk), "quantity": "1"})
    form = quotes.ESTIMATORS["generic"].form_class()(
        {"description": "Pulls", "quantity": "2", "labor_hours": "0.5"}
    )
    assert form.is_valid(), form.errors
    quotes.save_estimate(quotes.estimate_line(drawer, "generic", form.cleaned_data))
    _line(
        drawer,
        "adhoc",
        {"description": "No glass top", "quantity": "1", "unit": "each", "unit_price": "-200"},
        credit=True,
    )
    drawer.refresh_from_db()
    lines = list(drawer.lines.all())
    assert [line.kind for line in lines] == ["adhoc", "machine", "estimate", "adhoc"]
    assert lines[-1].line_total == D("-200.00")
    assert drawer.lines_total == sum(line.line_total for line in lines)

    # An ordinary quote never takes a negative price.
    plain = LINE_FORMS["adhoc"](
        {"description": "x", "quantity": "1", "unit": "each", "unit_price": "-5"}
    )
    assert not plain.is_valid()


@pytest.mark.django_db
def test_approval_needs_no_agreement_and_takes_amended_terms(admin, job, drawer):
    drawer.amended_terms = "Drawer slides are warranted by their maker."
    drawer.deposit_kind, drawer.deposit_value = "percent", D("50")
    drawer.save()
    quotes.recalculate(drawer)
    agreements_before = job.agreements.count()

    _approve(drawer, admin)  # no accept_agreement: the job's agreement stands
    approval = drawer.approval
    assert approval.agreement is not None and approval.agreement.status == "accepted"
    assert not approval.agreement.is_primary
    assert "warranted" in approval.agreement.text
    assert job.agreements.count() == agreements_before + 1
    job.refresh_from_db()
    assert job.status == "approved"  # unchanged

    deposit = Invoice.objects.get(job=job, kind=Invoice.Kind.DEPOSIT, source_revision=drawer)
    line = deposit.lines.get()
    assert line.unit_price == D("75.00") and "change order" in line.description


@pytest.mark.django_db
def test_without_terms_nothing_is_accepted_and_a_credit_takes_no_deposit(admin, job):
    credit = quotes.create_quote(job, title="Smaller top")
    _line(
        credit,
        "adhoc",
        {"description": "Smaller top", "quantity": "1", "unit": "each", "unit_price": "-100"},
        credit=True,
    )
    credit.deposit_kind, credit.deposit_value = "fixed", D("50")
    credit.save()
    quotes.recalculate(credit)
    credit.refresh_from_db()
    assert credit.total == D("-100.00") and credit.deposit_amount == 0
    _approve(credit, admin)
    assert credit.approval.agreement is None
    assert not Invoice.objects.filter(source_revision=credit).exists()


@pytest.mark.django_db
def test_approved_change_order_joins_the_job_s_one_work_order(admin, job, drawer):
    from trammel.stock import services as stock
    from trammel.stock.models import BOMLine

    wo = wos.create(job, revision=job.quotes.get(kind="quote").revisions.get())
    oil = Material.objects.create(name="Danish oil", unit="l", unit_cost=D("40"))
    pull = Product.objects.create(name="Brass pull", kind="made", price=D("12"))
    stock.save_bom_line(BOMLine(product=pull, material=oil, quantity=D("10"), unit="ml"))
    _line(drawer, "product", {"product": str(pull.pk), "quantity": "2"})
    _line(
        drawer,
        "adhoc",
        {"description": "Discount", "quantity": "1", "unit": "each", "unit_price": "-10"},
        credit=True,
    )
    _approve(drawer, admin)

    parent = wo.operations.get(source_revision=drawer)
    assert parent.name == f"{drawer.quote.number}: Add a drawer"
    steps = list(parent.children.order_by("position"))
    assert [s.name for s in steps] == ["Drawer", "Brass pull × 2"]  # the credit is no work
    planned = wo.planned_materials.get(material=oil)
    assert planned.operation == steps[1] and planned.quantity == D("20")
    with pytest.raises(ValidationError, match="already"):
        wos.add_change_order(wo, drawer)
    assert wos.pending_change_orders(job) == []


@pytest.mark.django_db
def test_with_two_work_orders_it_is_added_by_hand(admin_client, admin, job, drawer):
    first = wos.create(job)
    second = wos.create(job)
    _approve(drawer, admin)
    assert not first.operations.exists() and not second.operations.exists()
    assert wos.pending_change_orders(job) == [drawer]

    page = admin_client.get(f"/work-orders/{second.pk}/?tab=change-orders").content.decode()
    assert drawer.quote.number in page and "Add to this work order" in page
    response = admin_client.post(
        f"/work-orders/{second.pk}/change-orders/", {"revision": str(drawer.pk)}
    )
    assert response.status_code == 302
    assert second.operations.filter(source_revision=drawer).exists()
    assert wos.pending_change_orders(job) == []


@pytest.mark.django_db
def test_a_work_order_made_later_brings_approved_change_orders(admin, job, drawer):
    _approve(drawer, admin)
    wo = wos.create(job, revision=job.quotes.get(kind="quote").revisions.get())
    assert wo.operations.filter(source_revision=drawer).exists()
    assert wo.operations.filter(parent=None).count() == 2  # the table, and the drawer


@pytest.mark.django_db
def test_final_invoice_bills_it_once_and_costing_counts_it(admin, job, drawer):
    _approve(drawer, admin)
    invoice = billing.create(job.customer, kind=Invoice.Kind.FINAL, job=job)
    for revision in QuoteRevision.objects.filter(quote__job=job, status="approved"):
        billing.add_from_quote(invoice, revision)
    descriptions = list(invoice.lines.values_list("description", flat=True))
    assert f"{drawer.quote.number}: Drawer" in descriptions and "Table" in descriptions
    assert billing.add_from_quote(invoice, drawer) == 0  # never twice

    costing = reports.job_costing(job)
    assert sum(costing.quoted.values()) == D("1350.00")


@pytest.mark.django_db
def test_approving_a_change_order_needs_its_own_permission(client, admin, job, drawer):
    quotes.send(drawer, actor=admin)
    quoter = make_user("q@example.com", "quotes.view", "quotes.approve")
    with pytest.raises(PermissionDenied):
        quotes.approve(drawer, actor=quoter, approved_by_name="Grace")
    client.force_login(quoter)
    assert client.get(f"/quotes/r/{drawer.pk}/approve/").status_code == 403

    changer = make_user("c@example.com", "quotes.view", "quotes.approve_change_orders")
    quotes.approve(drawer, actor=changer, approved_by_name="Grace")
    drawer.refresh_from_db()
    assert drawer.status == "approved"


@pytest.mark.django_db
def test_screens_show_change_orders(admin_client, job, drawer):
    job_page = admin_client.get(f"/jobs/{job.pk}/?tab=quotes").content.decode()
    assert "New change order" in job_page and drawer.quote.number in job_page
    listing = admin_client.get("/quotes/?kind=change_order").content.decode()
    assert drawer.quote.number in listing
    assert drawer.quote.number not in admin_client.get("/quotes/").content.decode()
    page = admin_client.get(f"/quotes/r/{drawer.pk}/").content.decode()
    assert "Change order" in page
    document = admin_client.get(f"/quotes/r/{drawer.pk}/document/").content.decode()
    assert "<h1>Change order</h1>" in document


@pytest.mark.django_db
def test_unsent_quotes_can_be_deleted_and_restored(admin_client, admin, customer):
    job = jobs.create_job(Job(title="Bench", customer=customer))
    revision = quotes.create_quote(job)
    response = admin_client.post(f"/quotes/r/{revision.pk}/delete/", {"reason": "Duplicate"})
    assert response.status_code == 302
    assert not Quote.objects.filter(pk=revision.quote_id).exists()
    assert not QuoteRevision.objects.filter(pk=revision.pk).exists()
    quote = Quote.all_objects.get(pk=revision.quote_id)
    quote.restore()
    assert QuoteRevision.objects.filter(pk=revision.pk).exists()

    _line(
        revision,
        "adhoc",
        {"description": "Bench", "quantity": "1", "unit": "each", "unit_price": "300"},
    )
    quotes.send(revision, actor=admin)
    page = admin_client.get(f"/quotes/r/{revision.pk}/delete/").content.decode()
    assert "can't be deleted" in page
    admin_client.post(f"/quotes/r/{revision.pk}/delete/")
    assert Quote.objects.filter(pk=revision.quote_id).exists()
    with pytest.raises(ValidationError):
        quotes.delete_quote(revision.quote)
