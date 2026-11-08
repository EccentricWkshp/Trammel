"""FR-BIL-1..6 and FR-TAX-3..9 on invoices."""

import datetime as dt
import json
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from trammel.billing import services
from trammel.billing.models import Invoice
from trammel.core.lifecycle import TransitionError
from trammel.core.models import BusinessProfile, ShopRates
from trammel.customers.models import Address
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.tax.models import Boundary, Jurisdiction, TaxCategory, TaxRate

D = Decimal
TODAY = dt.date(2026, 11, 7)


@pytest.fixture
def taxes(setup_done):
    """Shop in Tacoma (10.3%); Seattle at 10.35%."""
    business = BusinessProfile.load()
    business.postal_code = "98402"
    business.save()
    for code, name, rate, zip_ in (
        ("2717", "Tacoma", "10.3", "98402"),
        ("1726", "Seattle", "10.35", "98101"),
    ):
        j = Jurisdiction.objects.create(code=code, name=name)
        TaxRate.objects.create(jurisdiction=j, rate=D(rate), effective_from=dt.date(2020, 1, 1))
        Boundary.objects.create(
            zip_low=zip_ + "0000",
            zip_high=zip_ + "9999",
            jurisdiction_codes=[code],
            location_code=code,
            source="manual",
        )


@pytest.fixture
def draft(taxes, customer):
    invoice = services.create(customer, kind=Invoice.Kind.DIRECT_SALE)
    services.add_line(invoice, description="Cutting board", quantity=2, unit_price=D("45"))
    return invoice


@pytest.mark.django_db
def test_issue_numbers_snapshots_tax_and_freezes(draft, admin):
    invoice = services.issue(draft, actor=admin)
    assert invoice.number.startswith("INV-") and invoice.status == "issued"
    assert invoice.subtotal == D("90.00") and invoice.tax_total == D("9.27")  # 10.3% pickup
    assert invoice.total == D("99.27") and invoice.balance == D("99.27")
    assert invoice.tax_location_code == "2717" and invoice.taxes.get().rate == D("10.3")
    assert invoice.due_date == invoice.invoice_date + dt.timedelta(days=invoice.terms_days)
    invoice.notes = "changed"
    with pytest.raises(ValidationError):
        invoice.save()
    with pytest.raises(ValidationError):
        services.add_line(invoice, description="x", quantity=1, unit_price=1)
    # A later rate change never touches the issued invoice (FR-TAX-7).
    TaxRate.objects.filter(jurisdiction__code="2717").update(rate=D("11"))
    invoice.refresh_from_db()
    assert invoice.tax_total == D("9.27")


@pytest.mark.django_db
def test_invoice_numbers_are_gap_free_and_drafts_have_none(taxes, customer, admin):
    a = services.create(customer, kind="direct_sale")
    b = services.create(customer, kind="direct_sale")
    for inv in (a, b):
        services.add_line(inv, description="x", quantity=1, unit_price=D("10"))
    assert a.number == "" and b.number == ""
    second = services.issue(b, actor=admin)
    first = services.issue(a, actor=admin)
    assert int(first.number[-4:]) == int(second.number[-4:]) + 1


@pytest.mark.django_db
def test_delivery_uses_the_destination_rate(draft, customer, admin):
    addr = Address.objects.create(
        customer=customer,
        line1="1 Pike St",
        city="Seattle",
        postal_code="98101",
        is_default_shipping=True,
    )
    draft.fulfillment, draft.ship_to = "delivery", addr
    draft.save()
    invoice = services.issue(draft, actor=admin)
    assert invoice.tax_location_code == "1726" and invoice.tax_total == D("9.32")
    assert "Seattle" in invoice.ship_to_text


@pytest.mark.django_db
def test_unresolved_address_needs_a_manual_choice_and_is_flagged(draft, customer, admin):
    addr = Address.objects.create(
        customer=customer, line1="1 Main", city="Portland", postal_code="97201"
    )
    draft.fulfillment, draft.ship_to = "shipping", addr
    draft.save()
    with pytest.raises(ValidationError, match="by hand"):
        services.issue(draft, actor=admin)
    draft.manual_jurisdiction = Jurisdiction.objects.get(code="2717")
    draft.save()
    invoice = services.issue(draft, actor=admin)
    assert invoice.tax_flagged and invoice.tax_method == "manual"


@pytest.mark.django_db
def test_exempt_customers_and_nontaxable_categories(draft, customer, admin):
    labor = TaxCategory.objects.create(name="Exempt service", taxable=False)
    services.add_line(
        draft, description="Consultation", quantity=1, unit_price=D("50"), tax_category=labor
    )
    draft.refresh_from_db()
    assert draft.taxable_total == D("90.00")
    customer.tax_exempt, customer.tax_exempt_number = True, "RS-123"
    customer.save()
    invoice = services.issue(draft, actor=admin)
    assert invoice.tax_total == D("0") and invoice.exemption_number == "RS-123"
    assert invoice.tax_method == "exempt"


@pytest.mark.django_db
def test_partial_payment_overpayment_and_credit(draft, customer, admin):
    invoice = services.issue(draft, actor=admin)
    services.record_payment(
        customer, amount=D("50"), received_on=TODAY, method="cash", allocations={invoice: D("50")}
    )
    invoice.refresh_from_db()
    assert invoice.status == "partially_paid" and invoice.balance == D("49.27")
    services.record_payment(
        customer,
        amount=D("100"),
        received_on=TODAY,
        method="check",
        allocations={invoice: D("49.27")},
    )
    invoice.refresh_from_db()
    assert invoice.status == "paid" and invoice.balance == D("0")
    assert services.customer_credit(customer) == D("50.73")
    # The credit pays the next invoice.
    nxt = services.create(customer, kind="direct_sale")
    services.add_line(nxt, description="Coaster", quantity=1, unit_price=D("20"))
    nxt = services.issue(nxt, actor=admin)
    services.apply_credit(nxt, nxt.balance)
    nxt.refresh_from_db()
    assert nxt.status == "paid" and services.customer_credit(customer) == D("28.67")


@pytest.mark.django_db
def test_cannot_overapply_a_payment(draft, customer, admin):
    invoice = services.issue(draft, actor=admin)
    with pytest.raises(ValidationError, match="More was applied"):
        services.record_payment(
            customer,
            amount=D("10"),
            received_on=TODAY,
            method="cash",
            allocations={invoice: D("20")},
        )
    with pytest.raises(ValidationError, match="more than"):
        services.record_payment(
            customer,
            amount=D("200"),
            received_on=TODAY,
            method="cash",
            allocations={invoice: D("200")},
        )


@pytest.mark.django_db
def test_void_only_without_payments(draft, customer, admin):
    invoice = services.issue(draft, actor=admin)
    services.record_payment(
        customer, amount=D("1"), received_on=TODAY, method="cash", allocations={invoice: D("1")}
    )
    with pytest.raises(ValidationError, match="credit note"):
        services.void(invoice, actor=admin, reason="mistake")
    other = services.create(customer, kind="direct_sale")
    services.add_line(other, description="x", quantity=1, unit_price=D("5"))
    other = services.issue(other, actor=admin)
    with pytest.raises(TransitionError, match="reason"):
        services.void(other, actor=admin, reason="")
    services.void(other, actor=admin, reason="Duplicate")
    other.refresh_from_db()
    assert other.status == "void" and other.balance == 0


@pytest.mark.django_db
def test_credit_note_reduces_the_balance(draft, admin):
    invoice = services.issue(draft, actor=admin)
    note = services.create_credit_note(invoice, amount=D("20"), description="Scratched board")
    note = services.issue(note, actor=admin)
    invoice.refresh_from_db()
    assert note.number.startswith("CN-") and note.tax_total == 0
    assert invoice.credited_total == D("20.00") and invoice.balance == D("79.27")


@pytest.mark.django_db
def test_write_off_settles_the_invoice(draft, admin):
    invoice = services.issue(draft, actor=admin)
    with pytest.raises(ValidationError, match="reason"):
        services.write_off(invoice, reason=" ")
    services.write_off(invoice, reason="Customer disappeared", user=admin)
    invoice.refresh_from_db()
    assert invoice.status == "paid" and invoice.written_off == D("99.27")


@pytest.fixture
def approved_job(taxes, customer, admin):
    rates = ShopRates.load()
    rates.labor_hourly_rate = D("60")
    rates.save()
    job = jobs.create_job(Job(title="Table", customer=customer))
    rev = quotes.create_quote(job)
    form = LINE_FORMS["adhoc"](
        {"description": "Table", "quantity": "1", "unit": "each", "unit_price": "1000"}
    )
    assert form.is_valid()
    quotes.save_line(form.build(rev))
    rev.deposit_kind, rev.deposit_value = "percent", D("50")
    quotes.update_revision(rev, before={})
    quotes.send(rev, actor=admin)
    quotes.approve(rev, actor=admin, approved_by_name="Grace", accept_agreement=True, user=admin)
    rev.refresh_from_db()
    return job, rev


@pytest.mark.django_db
def test_deposit_invoice_on_approval_and_applied_to_final(approved_job, customer, admin):
    job, rev = approved_job
    deposit = job.invoices.get(kind="deposit")
    # Quote total includes the 10.3% tax estimate: 1103.00 → 551.50 deposit, not taxed itself.
    assert deposit.status == "issued" and deposit.total == D("551.50") and deposit.tax_total == 0
    services.record_payment(
        customer,
        amount=deposit.total,
        received_on=TODAY,
        method="card",
        allocations={deposit: deposit.total},
    )
    final = services.create(customer, kind="final", job=job)
    services.add_from_quote(final, rev)
    services.add_deposit_credits(final)
    assert services.add_from_quote(final, rev) == 0  # never billed twice
    final = services.issue(final, actor=admin)
    assert final.subtotal == D("448.50")  # 1000 − 551.50
    assert final.tax_total == D("103.00")  # tax on the full 1000; the deposit line is not taxed
    assert final.balance == D("551.50")


@pytest.mark.django_db
def test_unbilled_time_and_materials_are_pulled_once(approved_job, admin):
    from trammel.timetracking import services as time
    from trammel.workorders import services as wos

    job, rev = approved_job
    wo = wos.create(job, revision=rev)
    from django.utils import timezone

    time.manual(wo, kind="labor", minutes=D("90"), started_at=timezone.now(), user=admin)
    wos.record_material(
        wo,
        material=None,
        description="Glue",
        quantity=D("1"),
        unit="each",
        unit_cost=D("8"),
        user=admin,
    )
    invoice = services.create(job.customer, kind="progress", job=job)
    assert services.add_unbilled_time(invoice) == 1
    assert services.add_unbilled_materials(invoice) == 1
    assert services.add_unbilled_time(invoice) == 0
    labor = invoice.lines.get(category="labor")
    assert labor.quantity == D("1.5") and labor.unit_price == D("60")


@pytest.mark.django_db
def test_job_closeout_needs_settled_invoices(approved_job, admin, no_job_gates):
    from trammel.billing.wiring import invoices_settled
    from trammel.jobs import hooks

    hooks.check("closed")(invoices_settled)
    job, _rev = approved_job
    for target in ("in_progress", "completed"):
        Job.lifecycle.apply(job, target, actor=admin)
    with pytest.raises(TransitionError, match="outstanding"):
        jobs.transition(job, "closed", actor=admin)


@pytest.mark.django_db
def test_force_closeout_can_write_off(approved_job, admin):
    job, _rev = approved_job
    deposit = job.invoices.get(kind="deposit")
    jobs.force_close(
        job, actor=admin, reason="Abandoned", resolutions={f"invoice:{deposit.pk}": "write_off"}
    )
    deposit.refresh_from_db()
    assert deposit.status == "paid" and deposit.write_offs.get().from_force_closeout


@pytest.mark.django_db
def test_screens(admin_client, draft, customer):
    url = f"/invoices/{draft.pk}/"
    assert admin_client.get(url).status_code == 200
    admin_client.post(
        f"{url}lines/",
        {"description": "Stand", "quantity": "1", "unit": "each", "unit_price": "15"},
    )
    assert draft.lines.count() == 2
    admin_client.post(f"{url}issue/")
    draft.refresh_from_db()
    assert draft.status == "issued"
    admin_client.post(
        f"{url}pay/", {"received_on": "2026-11-07", "amount": "200", "method": "cash"}
    )
    draft.refresh_from_db()
    assert draft.status == "paid"
    doc = admin_client.get(f"{url}document/")
    assert draft.number in doc.content.decode() and "sandbox" in doc["Content-Security-Policy"]
    for path in (
        "/invoices/",
        "/invoices/?status=overdue",
        "/invoices/templates/",
        f"/invoices/payment/{customer.pk}/",
        f"/customers/{customer.pk}/",
    ):
        assert admin_client.get(path).status_code == 200, path


@pytest.mark.django_db
def test_api(admin_client, taxes, customer):
    created = admin_client.post(
        "/api/v1/invoices/",
        json.dumps({"customer": str(customer.pk), "kind": "direct_sale"}),
        content_type="application/json",
    ).json()
    admin_client.post(
        f"/api/v1/invoices/{created['id']}/lines",
        json.dumps({"description": "Sign", "unit_price": "100"}),
        content_type="application/json",
    )
    issued = admin_client.post(f"/api/v1/invoices/{created['id']}/issue").json()
    assert issued["total"] == "110.30" and issued["taxes"][0]["code"] == "2717"
    paid = admin_client.post(
        f"/api/v1/invoices/{created['id']}/payments",
        json.dumps({"amount": "110.30", "received_on": "2026-11-07"}),
        content_type="application/json",
    ).json()
    assert paid["status"] == "paid"


@pytest.mark.django_db
def test_documents_print_whole_quantities_plainly(draft):
    services.add_line(draft, description="Coaster set", quantity=D("120"), unit_price=D("21.60"))
    services.add_line(draft, description="Walnut", quantity=D("2.5000"), unit_price=D("14"))
    html = services.render_html(draft)
    assert "1.2E+2" not in html and "120 each" in html and "2.5 each" in html
