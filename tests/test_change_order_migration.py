"""Change orders made before 0.4.3 become change-order quotes on upgrade."""

from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

D = Decimal
BEFORE = [
    ("workorders", "0006_operation_source_revision"),
    ("billing", "0007_template_archive"),
    ("quotes", "0008_change_orders"),
]


def _migrate(targets):
    """Migrate to the targets; return the models as every applied migration leaves them."""
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    executor = MigrationExecutor(connection)
    return executor._create_project_state(with_applied_migrations=True).apps


def _latest():
    return MigrationExecutor(connection).loader.graph.leaf_nodes()


@pytest.mark.django_db(transaction=True)
def test_old_change_orders_carry_over():
    try:
        _check(_migrate(BEFORE))
    finally:
        _migrate(_latest())  # never leave the test database behind


def _check(apps):
    Customer = apps.get_model("customers", "Customer")
    Job = apps.get_model("jobs", "Job")
    ChangeOrder = apps.get_model("workorders", "ChangeOrder")
    ChangeOrderLine = apps.get_model("workorders", "ChangeOrderLine")
    WorkOrder = apps.get_model("workorders", "WorkOrder")
    Operation = apps.get_model("workorders", "Operation")
    Invoice = apps.get_model("billing", "Invoice")
    InvoiceLine = apps.get_model("billing", "InvoiceLine")
    CustomerAgreement = apps.get_model("agreements", "CustomerAgreement")

    customer = Customer.objects.create(name="Grace Hopper")
    job = Job.objects.create(number="J-1", title="Table", customer=customer, status="in_progress")
    terms = apps.get_model("agreements", "AgreementTemplate").objects.create(name="Old terms")
    version = apps.get_model("agreements", "AgreementTemplateVersion").objects.create(
        template=terms, version=1, body="Terms."
    )
    agreement = CustomerAgreement.objects.create(
        job=job,
        template_version=version,
        is_primary=False,
        text="Amended terms — CO-1\n\nSlides warranted.",
        status="accepted",
    )
    approved = ChangeOrder.objects.create(
        job=job,
        number="CO-1",
        title="Add a drawer",
        description="Left side",
        amended_terms="Slides warranted.",
        status="approved",
        total=D("150.00"),
        approved_by_name="Grace",
        approved_at=timezone.now(),
        approval_how="phone",
        agreement=agreement,
    )
    line = ChangeOrderLine.objects.create(
        change_order=approved,
        description="Drawer",
        quantity=1,
        unit_price=D("150"),
        unit_cost=D("40"),
        line_total=D("150.00"),
        category="labor",
    )
    draft = ChangeOrder.objects.create(job=job, number="CO-2", title="Paint", total=D("0"))
    wo = WorkOrder.objects.create(job=job, number="WO-1", title="Table")
    op = Operation.objects.create(work_order=wo, name="Change order CO-1: Add a drawer")
    invoice = Invoice.objects.create(customer=customer, job=job)
    billed = InvoiceLine.objects.create(
        invoice=invoice,
        description="CO-1: Drawer",
        source_type="change_order_line",
        source_id=line.id,
    )

    apps = _migrate(_latest())
    Quote = apps.get_model("quotes", "Quote")
    QuoteLine = apps.get_model("quotes", "QuoteLine")
    Approval = apps.get_model("quotes", "Approval")

    quote = Quote.objects.get(pk=approved.pk)
    assert (quote.kind, quote.number, quote.title) == ("change_order", "CO-1", "Add a drawer")
    revision = quote.revisions.get()
    assert revision.status == "approved" and revision.total == D("150.00")
    assert revision.amended_terms == "Slides warranted." and revision.description == "Left side"
    moved = QuoteLine.objects.get(pk=line.pk)
    assert moved.revision_id == revision.pk and moved.split == {"labor": "150.00"}
    assert moved.cost_total == D("40.00")
    approval = Approval.objects.get(revision=revision)
    assert (approval.approved_by_name, approval.how) == ("Grace", "phone")
    assert approval.agreement_id == agreement.pk
    assert Quote.objects.get(pk=draft.pk).revisions.get().status == "draft"
    assert apps.get_model("workorders", "Operation").objects.get(pk=op.pk).source_revision_id == (
        revision.pk
    )
    assert apps.get_model("billing", "InvoiceLine").objects.get(pk=billed.pk).source_type == (
        "quote_line"
    )
