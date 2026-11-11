"""Optional and alternate quote lines (FR-QUO-11): left out of the total until
chosen, picked when approving (by staff, in the shop or in the portal), and
only what was chosen flows on to the deposit, the work order and the invoice."""

import re
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from tests.test_portal import grace, portal_on, signed_in, smtp  # noqa: F401
from trammel.billing import services as billing
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.quotes.models import Approval, QuoteRevision
from trammel.workorders import services as wos

D = Decimal


def _item(revision, description, price):
    form = LINE_FORMS["adhoc"](
        {"description": description, "quantity": "1", "unit": "each", "unit_price": price}
    )
    assert form.is_valid(), form.errors
    return quotes.save_line(form.build(revision))


@pytest.fixture
def table(customer):
    """A table quote: the build, a choice of wood (walnut by default), and benches
    as an option, with a 50% deposit."""
    job = jobs.create_job(Job(title="Dining table", customer=customer))
    revision = quotes.create_quote(job)
    revision.deposit_kind, revision.deposit_value = "percent", D("50")
    revision.save()
    lines = {
        "build": _item(revision, "Build and finish", "1000"),
        "walnut": _item(revision, "Walnut top", "800"),
        "maple": _item(revision, "Maple top", "500"),
        "benches": _item(revision, "Two benches", "900"),
    }
    quotes.set_offer(lines["maple"], "alternate", "Wood")
    quotes.set_offer(lines["walnut"], "alternate", "wood", default=True)
    quotes.set_offer(lines["benches"], "optional")
    revision.refresh_from_db()
    return revision, {k: v.__class__.objects.get(pk=v.pk) for k, v in lines.items()}


def _sent(table, admin):
    revision, lines = table
    quotes.send(revision, actor=admin)
    revision.refresh_from_db()
    return revision, lines


@pytest.mark.django_db
def test_options_and_other_alternatives_stay_out_of_the_total(table):
    revision, lines = table
    assert lines["walnut"].group == "Wood"  # joined the group as it was first written
    assert [lines[k].included for k in ("build", "walnut", "maple", "benches")] == [
        True,
        True,
        False,
        False,
    ]
    assert revision.lines_total == D("1800")
    # Making maple the default swaps the two.
    quotes.set_offer(lines["maple"], "alternate", "Wood", default=True)
    revision.refresh_from_db()
    assert revision.lines_total == D("1500")
    assert quotes.groups(revision) == ["Wood"]
    with pytest.raises(ValidationError, match="Name the group"):
        quotes.set_offer(lines["build"], "alternate", "  ")
    # Deleting the default leaves the group's other line chosen.
    quotes.delete_line(QuoteRevision.objects.get(pk=revision.pk).lines.get(description="Maple top"))
    revision.refresh_from_db()
    assert revision.lines.get(description="Walnut top").included
    assert revision.lines_total == D("1800")


@pytest.mark.django_db
def test_the_customers_choices_become_the_scope_at_approval(table, admin):
    revision, lines = _sent(table, admin)
    quotes.approve(
        revision,
        actor=admin,
        approved_by_name="Grace",
        accept_agreement=True,
        picks=[str(lines["maple"].pk), str(lines["benches"].pk)],
    )
    revision.refresh_from_db()
    assert revision.lines_total == D("2400")  # build, maple and the benches
    assert set(revision.included_lines().values_list("description", flat=True)) == {
        "Build and finish",
        "Maple top",
        "Two benches",
    }
    snapshot = Approval.objects.get(revision=revision).snapshot
    assert snapshot["totals"]["total"] == str(revision.total)
    assert {(x["description"], x["included"]) for x in snapshot["lines"]} >= {
        ("Walnut top", False),
        ("Maple top", True),
    }

    # The deposit is half of the chosen total.
    job = revision.quote.job
    deposit = job.invoices.get(kind="deposit")
    assert deposit.lines.get().unit_price == revision.deposit_for(revision.total)

    # Only what was chosen becomes work, and gets billed.
    wo = wos.create(job, revision=revision)
    names = set(wo.operations.values_list("name", flat=True))
    assert "Maple top" in names and "Two benches" in names and "Walnut top" not in names
    final = billing.create(job.customer, kind="final", job=job)
    billing.add_from_quote(final, revision)
    billed = set(final.lines.values_list("description", flat=True))
    assert {"Maple top", "Two benches"} <= billed and "Walnut top" not in billed


@pytest.mark.django_db
def test_without_picks_the_defaults_stand(table, admin):
    revision, _ = _sent(table, admin)
    total = revision.total
    quotes.approve(revision, actor=admin, approved_by_name="Grace", accept_agreement=True)
    revision.refresh_from_db()
    assert revision.total == total and revision.lines_total == D("1800")


@pytest.mark.django_db
def test_the_document_lists_what_is_left_out_and_what_it_would_change(table, admin):
    revision, lines = _sent(table, admin)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", quotes.render_html(revision)))
    assert "Options and alternatives, not in the total" in text
    assert "Wood: Maple top" in text and "−$300.00" in text
    assert "Two benches" in text and "+$900.00" in text
    assert "(Wood: our suggestion)" in text
    quotes.approve(
        revision,
        actor=admin,
        approved_by_name="Grace",
        accept_agreement=True,
        picks=[str(lines["maple"].pk)],
    )
    revision.refresh_from_db()
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", quotes.render_html(revision)))
    assert "Not chosen" in text and "Wood: Walnut top" in text and "(Wood: chosen)" in text


@pytest.mark.django_db
def test_staff_record_the_choices_with_the_approval(admin_client, table, admin):
    revision, lines = _sent(table, admin)
    url = f"/quotes/r/{revision.pk}/approve/"
    page = admin_client.get(url).content.decode()
    assert "The customer's choices" in page and "Walnut top" in page
    page = admin_client.get(f"{url}?pick-1={lines['maple'].pk}&pick={lines['benches'].pk}")
    page = page.content.decode()
    # The total for those choices, and the picks sent on with the approval.
    figures = quotes.totals_with(
        revision, {lines["build"].pk, lines["maple"].pk, lines["benches"].pk}
    )
    assert f"Record approval for ${figures['total']:,.2f}" in page
    assert page.count('type="hidden" name="pick"') == 2
    # Handed over the counter to sign, the customer picks for themselves.
    page = admin_client.get(f"/quotes/r/{revision.pk}/sign/").content.decode()
    assert "Your choices" in page and "Two benches" in page
    admin_client.post(
        url,
        {
            "approved_by_name": "Grace",
            "approved_at": "2026-11-11T10:00",
            "how": "phone",
            "accept_agreement": "on",
            "pick": [str(lines["maple"].pk), str(lines["benches"].pk)],
        },
    )
    revision.refresh_from_db()
    assert revision.status == "approved" and revision.total == figures["total"]


@pytest.mark.django_db
def test_the_customer_picks_in_the_portal(signed_in, grace, admin):  # noqa: F811
    from trammel.agreements.models import AgreementSettings

    # Typed and ticked; signing is in test_signatures.py.
    agreement_settings = AgreementSettings.load()
    agreement_settings.signature_enabled = False
    agreement_settings.save()
    job = jobs.create_job(Job(title="Bench", customer=grace))
    revision = quotes.create_quote(job)
    _item(revision, "Bench", "400")
    cushion = _item(revision, "Cushion", "60")
    quotes.set_offer(cushion, "optional")
    quotes.send(revision, actor=admin)
    url = f"/portal/quotes/{revision.pk}/"
    page = signed_in.get(url).content.decode()
    assert "Your choices" in page and "+$60.00" in page
    page = signed_in.get(f"{url}?pick={cushion.pk}").content.decode()
    terms = re.search(r'name="terms" value="([0-9a-f]+)"', page).group(1)
    assert f'name="pick" value="{cushion.pk}"' in page
    signed_in.post(
        f"{url}respond/",
        {
            "answer": "approve",
            "name": "Grace Hopper",
            "terms": terms,
            "accept": "1",
            "pick": str(cushion.pk),
        },
    )
    revision.refresh_from_db()
    assert revision.status == "approved" and revision.lines_total == D("460")


@pytest.mark.django_db
def test_offers_carry_to_the_next_revision_and_through_the_api(admin_client, table, admin):
    revision, lines = _sent(table, admin)
    new = quotes.revise(revision, actor=admin)
    assert {(x.description, x.offer, x.group, x.included) for x in new.lines.all()} == {
        ("Build and finish", "included", "", True),
        ("Walnut top", "alternate", "Wood", True),
        ("Maple top", "alternate", "Wood", False),
        ("Two benches", "optional", "", False),
    }
    line = new.lines.get(description="Two benches")
    body = admin_client.post(
        f"/api/v1/quotes/{new.pk}/lines/{line.pk}/offer",
        {"offer": "included"},
        content_type="application/json",
    ).json()
    assert body["lines_total"] in ("2700.00", "2700")
    row = next(x for x in body["lines"] if x["description"] == "Two benches")
    assert (row["offer"], row["included"]) == ("included", True)


@pytest.mark.django_db
def test_offered_as_is_set_from_the_quote_page(admin_client, table):
    revision, lines = table
    page = admin_client.get(revision.get_absolute_url()).content.decode()
    assert "Offered as…" in page and "Wood, default" in page and "Option" in page
    url = f"/quotes/r/{revision.pk}/lines/{lines['build'].pk}/offer/"
    assert 'list="offer-groups"' in admin_client.get(url).content.decode()
    page = admin_client.post(url, {"offer": "alternate", "group": ""}).content.decode()
    assert "Name the group" in page
    admin_client.post(url, {"offer": "optional"})
    revision.refresh_from_db()
    assert revision.lines_total == D("800")


@pytest.mark.django_db
def test_the_document_follows_the_choices_while_picking(signed_in, table, admin, admin_client):  # noqa: F811
    revision, lines = _sent(table, admin)
    picks = f"pick={lines['maple'].pk}&pick={lines['benches'].pk}"
    page = signed_in.get(f"/portal/quotes/{revision.pk}/?{picks}").content.decode()
    # The document and the PDF are linked with the picks.
    assert f"/portal/quotes/{revision.pk}/document/?pick=" in page
    assert f"/portal/quotes/{revision.pk}/pdf/?pick=" in page and "with these choices" in page

    def document(url):
        html = (
            signed_in.get(url).content.decode()
            if "/portal/" in url
            else (admin_client.get(url).content.decode())
        )
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

    figures = quotes.totals_with(
        revision, {lines["build"].pk, lines["maple"].pk, lines["benches"].pk}
    )
    text = document(f"/portal/quotes/{revision.pk}/document/?{picks}")
    assert "(Wood: chosen)" in text and "(option, added)" in text
    assert f"${figures['total']:,.2f}" in text
    assert "Wood: Walnut top" in text  # now left out, and what it would add
    # The in-shop signing page shows the same.
    page = admin_client.get(f"/quotes/r/{revision.pk}/sign/?{picks}").content.decode()
    assert f"/quotes/r/{revision.pk}/document/?pick=" in page
    text = document(f"/quotes/r/{revision.pk}/document/?{picks}")
    assert f"${figures['total']:,.2f}" in text

    # Once approved, the document is what was approved, whatever the link says.
    quotes.approve(revision, actor=admin, approved_by_name="Grace", accept_agreement=True)
    revision.refresh_from_db()
    text = document(f"/portal/quotes/{revision.pk}/document/?{picks}")
    assert "(Wood: chosen)" in text and "Wood: Maple top" in text
    assert f"${revision.total:,.2f}" in text and "(option, added)" not in text
