"""FR-QUO-1..10: lines, estimators, totals, revisions, approval, expiry, templates."""

import datetime as dt
import json
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from trammel.agreements.models import AgreementSettings
from trammel.core.lifecycle import TransitionError
from trammel.core.models import ShopRates
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services
from trammel.quotes.estimators import ESTIMATORS
from trammel.quotes.forms import LINE_FORMS
from trammel.quotes.models import QuoteLine, QuoteRevision
from trammel.shop.models import Machine, Material

D = Decimal


@pytest.fixture
def rates(setup_done):
    r = ShopRates.load()
    r.labor_hourly_rate = D("60")
    r.shop_overhead_rate = D("0")
    r.default_material_markup = D("50")
    r.consumables_allowance = D("0")
    r.minimum_job_charge = D("0")
    r.save()
    return r


@pytest.fixture
def job(rates, customer):
    return jobs.create_job(Job(title="Walnut sign", customer=customer))


@pytest.fixture
def revision(job):
    return services.create_quote(job)


def add(revision, kind, **data):
    form = LINE_FORMS[kind](data)
    assert form.is_valid(), form.errors
    return services.save_line(form.build(revision))


@pytest.fixture
def laser(setup_done):
    return Machine.objects.create(name="Laser", kind="laser", hourly_rate=D("90"))


@pytest.fixture
def ply(setup_done):
    return Material.objects.create(
        name="Birch ply",
        category="sheet",
        unit="sheet",
        unit_cost=D("48"),
        sheet_width=24,
        sheet_length=12,
        sheet_dimension_unit="in",
    )


@pytest.mark.django_db
def test_create_numbers_and_snapshots_rates(revision, rates):
    assert revision.quote.number.startswith("Q-") and revision.number == 1
    assert revision.labor_rate == D("60") and revision.template is not None
    rates.labor_hourly_rate = D("99")
    rates.save()
    revision.refresh_from_db()
    assert revision.labor_rate == D("60")  # a rate change does not reprice the quote


@pytest.mark.django_db
def test_lines_totals_discount_and_minimum(revision):
    add(
        revision,
        "adhoc",
        description="Sign",
        quantity="2",
        unit="each",
        unit_price="100",
        discount_pct="10",
    )
    add(revision, "labor", description="Install", quantity="1.5")
    revision.refresh_from_db()
    assert revision.lines_total == D("270.00")  # 180 + 90
    revision.discount_pct = D("10")
    revision.minimum_charge = D("500")
    services.update_revision(revision, before={"discount_pct": "0", "minimum_charge": "0"})
    revision.refresh_from_db()
    assert revision.discount_amount == D("27.00")
    assert revision.minimum_adjustment == D("257.00") and revision.subtotal == D("500.00")
    assert revision.rates_overridden


@pytest.mark.django_db
def test_material_line_prices_with_markup(revision, ply):
    line = add(revision, "material", material=str(ply.pk), quantity="2", unit="sq_ft")
    # 48 per 2 ft² sheet → 24/ft² cost, +50% markup → 36/ft².
    assert line.unit_cost == D("24.0000") and line.unit_price == D("36.0000")
    assert line.line_total == D("72.00") and line.split == {"materials": "72.00"}


@pytest.mark.django_db
def test_laser_estimator_breakdown(revision, laser, ply):
    form = ESTIMATORS["laser"].form_class()(
        {
            "description": "Coasters",
            "quantity": "10",
            "material": str(ply.pk),
            "area": "16",
            "area_unit": "sq_in",
            "waste_pct": "0",
            "cut_minutes": "3",
            "engrave_minutes": "3",
            "machine": str(laser.pk),
            "labor_minutes": "0",
            "setup_minutes": "30",
        }
    )
    assert form.is_valid(), form.errors
    line = services.save_estimate(services.estimate_line(revision, "laser", form.cleaned_data))
    rows = {r["label"]: r for r in line.breakdown}
    # 160 in² of a 288 in² sheet at 48 → 26.67 cost, ×1.5 → 40.00 price.
    assert rows["Material (incl. waste)"]["cost"] == "26.67"
    assert rows["Material (incl. waste)"]["price"] == "40.00"
    assert rows["Laser time — Laser"]["price"] == "90.00"  # 60 minutes at 90/h
    assert rows["Setup / design"]["price"] == "30.00"
    assert line.line_total == D("160.00") and line.unit_price == D("16.0000")
    assert line.split == {"materials": "40.00", "machine": "90.00", "labor": "30.00"}


@pytest.mark.django_db
def test_print_estimator_applies_failure_allowance(revision, setup_done):
    printer = Machine.objects.create(name="Prusa", kind="printer_3d", hourly_rate=D("5"))
    pla = Material.objects.create(
        name="PLA", category="filament", unit="kg", unit_cost=D("20"), markup=D("0")
    )
    form = ESTIMATORS["print3d"].form_class()(
        {
            "description": "Bracket",
            "quantity": "4",
            "material": str(pla.pk),
            "grams": "50",
            "print_hours": "2",
            "machine": str(printer.pk),
            "failure_pct": "10",
            "post_hours": "0",
            "setup_hours": "0",
        }
    )
    assert form.is_valid(), form.errors
    result = ESTIMATORS["print3d"].estimate(form.cleaned_data, services.Rates.of(revision))
    by_label = {r["label"]: D(r["price"]) for r in result.breakdown}
    assert by_label["Material (incl. failure allowance)"] == D("4.40")  # 220 g at 0.02
    assert by_label["Print time — Prusa"] == D("44.00")  # 8.8 h at 5


@pytest.fixture
def plasma(setup_done):
    # Calculated cost: 20,000 over 10,000 h = 2.00/h; charged at 120/h.
    return Machine.objects.create(
        name="Plasma table",
        kind="plasma",
        hourly_rate=D("120"),
        purchase_cost=D("20000"),
        lifespan_hours=10000,
    )


@pytest.fixture
def plate(setup_done):
    return Material.objects.create(
        name="Mild steel 3/16", category="metal", unit="sq_ft", unit_cost=D("8"), markup=D("100")
    )


def _cutting(revision, **data):
    form = ESTIMATORS["cutting"].form_class()(
        {"description": "Brackets", "quantity": "10", "length_unit": "in", **data}
    )
    assert form.is_valid(), form.errors
    return ESTIMATORS["cutting"].estimate(form.cleaned_data, services.Rates.of(revision))


@pytest.mark.django_db
def test_cutting_estimator_by_machine_time(revision, plasma, plate):
    result = _cutting(
        revision,
        machine=str(plasma.pk),
        material=str(plate.pk),
        part_length="12",
        part_width="12",
        waste_pct="0",
        cut_length="50",
        pierces="3",
        pricing="time",
        feed_rate="100",
        pierce_seconds="2",
        cad_hours="0.5",
    )
    rows = {r["label"]: r for r in result.breakdown}
    # 10 ft² at 8 = 80 cost, 100% markup → 160.
    assert rows["Material (incl. waste)"]["cost"] == "80.00"
    assert rows["Material (incl. waste)"]["price"] == "160.00"
    # 500 in at 100 ipm = 5 min, + 30 pierces × 2 s = 1 min → 0.1 h at 120 = 12.00, cost 0.20.
    assert rows["Cutting — Plasma table"]["price"] == "12.00"
    assert rows["Cutting — Plasma table"]["cost"] == "0.20"
    assert rows["CAD/CAM and setup"]["price"] == "30.00"
    assert result.line_total == D("202.00")


@pytest.mark.django_db
def test_cutting_estimator_per_inch_and_pierce(revision, plasma):
    # The GoodBadUgly sheet's charges: 0.025 per inch, 0.10 per pierce.
    result = _cutting(
        revision,
        machine=str(plasma.pk),
        cut_length="50",
        pierces="3",
        pricing="length",
        price_per_length="0.025",
        price_per_pierce="0.10",
        feed_rate="100",
        pierce_seconds="2",
    )
    row = next(r for r in result.breakdown if r["label"].startswith("Cutting"))
    assert row["price"] == "15.50"  # 500 × 0.025 + 30 × 0.10
    assert row["cost"] == "0.20"  # still costed from machine time


@pytest.mark.django_db
def test_cutting_estimator_validates_its_pricing_inputs(revision, plasma, plate):
    form = ESTIMATORS["cutting"].form_class()(
        {
            "description": "x",
            "quantity": "1",
            "machine": str(plasma.pk),
            "material": str(plate.pk),
            "length_unit": "in",
            "cut_length": "10",
            "pricing": "time",
        }
    )
    assert not form.is_valid()
    assert "feed_rate" in form.errors and "part_length" in form.errors


@pytest.mark.django_db
def test_sent_revision_is_immutable(revision, admin):
    line = add(revision, "adhoc", description="X", quantity="1", unit="each", unit_price="10")
    services.send(revision, actor=admin)
    line.refresh_from_db()
    line.description = "changed"
    with pytest.raises(ValidationError):
        line.save()
    revision.refresh_from_db()
    revision.customer_notes = "changed"
    with pytest.raises(ValidationError):
        revision.save()
    with pytest.raises(ValidationError):
        services.save_line(QuoteLine(revision=revision, kind="adhoc", description="new"))


@pytest.mark.django_db
def test_cannot_send_empty(revision, admin):
    with pytest.raises(TransitionError, match="at least one line"):
        services.send(revision, actor=admin)


@pytest.mark.django_db
def test_send_moves_job_to_quoted_and_revise_supersedes(revision, admin, job):
    add(revision, "adhoc", description="X", quantity="1", unit="each", unit_price="10")
    services.send(revision, actor=admin)
    job.refresh_from_db()
    assert job.status == "quoted"
    new = services.revise(revision, actor=admin)
    revision.refresh_from_db()
    assert revision.status == "superseded" and new.number == 2 and new.lines.count() == 1
    with pytest.raises(ValidationError):
        services.revise(revision, actor=admin)


@pytest.mark.django_db
def test_approval_requires_the_agreement_and_moves_the_job(revision, admin, job):
    add(revision, "adhoc", description="X", quantity="1", unit="each", unit_price="100")
    revision.deposit_kind, revision.deposit_value = "percent", D("50")
    services.update_revision(revision, before={})
    services.send(revision, actor=admin)
    with pytest.raises(ValidationError, match="agreement"):
        services.approve(revision, actor=admin, approved_by_name="Grace")
    approval = services.approve(
        revision,
        actor=admin,
        approved_by_name="Grace",
        how="phone",
        accept_agreement=True,
        user=admin,
    )
    job.refresh_from_db()
    revision.refresh_from_db()
    assert revision.status == "approved" and job.status == "approved"
    assert approval.agreement.status == "accepted" and "TERMS" in approval.agreement_text
    assert approval.snapshot["totals"]["total"] == "100.00"
    assert revision.deposit_amount == D("50.00")
    with pytest.raises(ValidationError):
        services.revise(revision, actor=admin)


@pytest.mark.django_db
def test_approval_without_agreement_when_not_required(revision, admin):
    settings = AgreementSettings.load()
    settings.required = False
    settings.save()
    add(revision, "adhoc", description="X", quantity="1", unit="each", unit_price="10")
    services.send(revision, actor=admin)
    approval = services.approve(revision, actor=admin, approved_by_name="Grace")
    assert approval.agreement is None


@pytest.mark.django_db
def test_job_cannot_be_marked_quoted_without_a_quote(job, admin):
    with pytest.raises(TransitionError, match="Send a quote"):
        jobs.transition(job, "quoted", actor=admin)


@pytest.mark.django_db
def test_expiry(revision, admin):
    add(revision, "adhoc", description="X", quantity="1", unit="each", unit_price="10")
    services.send(revision, actor=admin)
    assert services.expire_due(revision.valid_until) == 0
    assert services.expire_due(revision.valid_until + dt.timedelta(days=1)) == 1
    revision.refresh_from_db()
    assert revision.status == "expired"


@pytest.mark.django_db
def test_document_renders_in_a_sandbox(admin_client, revision):
    add(
        revision,
        "adhoc",
        description="<script>alert(1)</script> Sign",
        quantity="1",
        unit="each",
        unit_price="10",
    )
    response = admin_client.get(f"/quotes/r/{revision.pk}/document/")
    body = response.content.decode()
    assert "&lt;script&gt;" in body and revision.quote.number in body
    csp = response["Content-Security-Policy"]
    assert "sandbox" in csp and "default-src 'none'" in csp
    assert response["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.django_db
def test_duplicate_job_copies_quote_lines(revision, admin, job):
    add(revision, "adhoc", description="Sign", quantity="1", unit="each", unit_price="10")
    copy = jobs.duplicate_job(job)
    new_rev = QuoteRevision.objects.get(quote__job=copy)
    assert new_rev.status == "draft" and new_rev.lines.get().description == "Sign"


@pytest.mark.django_db
def test_screens(admin_client, revision, laser, ply):
    url = f"/quotes/r/{revision.pk}/"
    assert admin_client.get(url).status_code == 200
    for kind in LINE_FORMS:
        assert admin_client.get(f"{url}lines/new/{kind}/").status_code == 200, kind
    for key in ESTIMATORS:
        assert admin_client.get(f"{url}estimate/{key}/").status_code == 200, key
    response = admin_client.post(
        f"{url}lines/new/adhoc/",
        {
            "description": "Panel",
            "quantity": "1",
            "unit": "each",
            "unit_price": "25",
            "unit_cost": "5",
            "discount_pct": "0",
        },
    )
    assert response.status_code == 302 and revision.lines.count() == 1
    preview = admin_client.post(
        f"{url}estimate/laser/",
        {
            "description": "Tags",
            "quantity": "1",
            "area": "0",
            "area_unit": "sq_in",
            "sheets": "1",
            "material": str(ply.pk),
            "machine": str(laser.pk),
            "cut_minutes": "6",
            "preview": "1",
        },
    )
    assert "Laser time" in preview.content.decode() and revision.lines.count() == 1
    admin_client.post(f"{url}send/")
    revision.refresh_from_db()
    assert revision.status == "sent"
    assert admin_client.get(f"{url}approve/").status_code == 200
    for path in (
        "/quotes/",
        "/quotes/templates/",
        "/quotes/settings/",
        f"/jobs/{revision.quote.job_id}/",
    ):
        assert admin_client.get(path).status_code == 200, path


@pytest.mark.django_db
def test_template_with_syntax_error_is_refused(admin_client):
    response = admin_client.post("/quotes/templates/new/", {"name": "Bad", "body": "{% for %}"})
    assert response.status_code == 200 and "Line 1" in response.content.decode()


@pytest.mark.django_db
def test_api_flow(admin_client, job, laser):
    created = admin_client.post(
        "/api/v1/quotes/", json.dumps({"job": str(job.pk)}), content_type="application/json"
    )
    rid = created.json()["id"]
    added = admin_client.post(
        f"/api/v1/quotes/{rid}/estimates",
        json.dumps(
            {
                "estimator": "generic",
                "inputs": {
                    "description": "Shelf",
                    "quantity": "1",
                    "labor_hours": "2",
                    "machine": str(laser.pk),
                    "machine_hours": "1",
                },
            }
        ),
        content_type="application/json",
    )
    assert added.status_code == 201 and added.json()["total"] == "210.00"
    bad = admin_client.post(
        f"/api/v1/quotes/{rid}/lines",
        json.dumps({"kind": "adhoc", "data": {"quantity": "1"}}),
        content_type="application/json",
    )
    assert bad.status_code == 422
    sent = admin_client.post(f"/api/v1/quotes/{rid}/send")
    assert sent.json()["status"] == "sent"
