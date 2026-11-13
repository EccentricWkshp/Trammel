"""Signature capture (FR-AGR-7) in the portal and on a shop device, and notes
shared with the customer (FR-PRT-4)."""

import base64
import io
import re

import pytest
from django.core.exceptions import ValidationError
from PIL import Image, ImageDraw

from tests.test_portal import _sent_quote, grace, portal_on, signed_in, smtp  # noqa: F401
from trammel.agreements.models import AgreementSettings, CustomerAgreement
from trammel.core import signature
from trammel.customers.models import Customer
from trammel.quotes.models import Approval
from trammel.workorders import services as wos


def drawn(width=500, height=160, ink=True) -> str:
    """A signature as a pad sends it: a transparent PNG with a line drawn on it."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if ink:
        ImageDraw.Draw(image).line(
            [(40, 120), (180, 40), (320, 110), (460, 50)], fill="black", width=3
        )
    out = io.BytesIO()
    image.save(out, "PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


def test_a_drawing_is_cropped_to_the_ink_and_kept_as_a_png():
    png = signature.clean(drawn())
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG" and image.width < 500 and image.height < 160


@pytest.mark.parametrize(
    "sent",
    [
        "",
        drawn(ink=False),  # an empty pad
        "data:image/png;base64,not-base64!",
        "data:image/jpeg;base64," + base64.b64encode(b"x").decode(),
        "data:image/png;base64," + base64.b64encode(b"GIF89a junk").decode(),
        drawn(width=5000, height=10),
    ],
)
def test_no_signature_or_a_bad_one_is_refused(sent):
    with pytest.raises(ValidationError):
        signature.clean(sent)


def _terms(page: str) -> str:
    return re.search(r'name="terms" value="([0-9a-f]+)"', page).group(1)


@pytest.mark.django_db
def test_customers_sign_to_approve_in_the_portal(signed_in, grace, admin):  # noqa: F811
    revision = _sent_quote(grace, admin)
    url = f"/portal/quotes/{revision.pk}/"
    page = signed_in.get(url).content.decode()
    assert 'data-signature="signature"' in page and "signature.js" in page
    answer = {"answer": "approve", "name": "Grace Hopper", "terms": _terms(page), "accept": "1"}
    # Unsigned, nothing is approved.
    assert (
        "Sign in the box" in signed_in.post(f"{url}respond/", answer, follow=True).content.decode()
    )
    revision.refresh_from_db()
    assert revision.status == "sent"
    signed_in.post(f"{url}respond/", {**answer, "signature": drawn()}, REMOTE_ADDR="203.0.113.7")
    revision.refresh_from_db()
    assert revision.status == "approved"
    approval = Approval.objects.get(revision=revision)
    agreement = CustomerAgreement.objects.get(job=revision.quote.job, status="accepted")
    assert approval.method == "portal" and bytes(approval.signature)[:4] == b"\x89PNG"
    assert agreement.method == "signature" and bytes(agreement.signature) == bytes(
        approval.signature
    )
    assert agreement.evidence_ip == "203.0.113.7"


@pytest.mark.django_db
def test_signing_on_a_shop_device(admin_client, admin, customer, smtp):  # noqa: F811
    revision = _sent_quote(customer, admin)
    page = admin_client.get(revision.get_absolute_url()).content.decode()
    assert "Sign on this device" in page
    sign_url = f"/quotes/r/{revision.pk}/sign/"
    page = admin_client.get(sign_url).content.decode()
    # Made to hand over the counter: no staff menus.
    assert "mainnav" not in page and "Approve and sign" in page and "Customer agreement" in page
    response = admin_client.post(
        sign_url,
        {"name": "Grace Hopper", "terms": _terms(page), "accept": "1", "signature": drawn()},
    )
    assert "Thank you, Grace Hopper" in response.content.decode()
    revision.refresh_from_db()
    approval = Approval.objects.get(revision=revision)
    assert (revision.status, approval.method, approval.how) == ("approved", "signed", "in_person")
    assert approval.recorded_by == admin and approval.signature
    agreement = CustomerAgreement.objects.get(job=revision.quote.job, status="accepted")
    assert agreement.method == "signature" and agreement.signature
    # On the printable agreement, which works where PDFs can't be made.
    printable = admin_client.get(f"/agreements/{agreement.pk}/print/?print=1")
    assert printable.status_code == 200
    assert 'class="signature" src="data:image/png;base64,' in printable.content.decode()
    assert "Print</a>" in admin_client.get(agreement.get_absolute_url()).content.decode()
    # And on the quote document, which its PDF is made from.
    from trammel.quotes import services as quotes

    document = quotes.render_html(revision)
    assert "Approved by Grace Hopper" in document and "accepting the terms above" in document
    assert 'class="signature" src="data:image/png;base64,' in document
    # The signature shows with the approval and on the agreement.
    assert (
        'alt="Signature of Grace Hopper"'
        in admin_client.get(revision.get_absolute_url()).content.decode()
    )
    assert (
        'alt="Signature of Grace Hopper"'
        in admin_client.get(agreement.get_absolute_url()).content.decode()
    )


@pytest.mark.django_db
def test_no_signing_when_signature_capture_is_off(admin_client, admin, customer):
    settings_ = AgreementSettings.load()
    settings_.signature_enabled = False
    settings_.save()
    revision = _sent_quote(customer, admin)
    assert (
        "Sign on this device" not in admin_client.get(revision.get_absolute_url()).content.decode()
    )
    assert admin_client.get(f"/quotes/r/{revision.pk}/sign/").status_code == 302


def _approved_work_order(customer, admin):
    from trammel.quotes import services as quotes

    revision = _sent_quote(customer, admin)
    quotes.approve(revision, actor=admin, approved_by_name="Grace", accept_agreement=True)
    return wos.create(revision.quote.job, revision=revision)


@pytest.fixture
def work_order(grace, admin):  # noqa: F811
    return _approved_work_order(grace, admin)


@pytest.mark.django_db
def test_notes_shared_with_the_customer_show_in_the_portal(
    signed_in,  # noqa: F811
    admin_client,
    work_order,
    admin,  # noqa: F811
):
    job_url = f"/portal/jobs/{work_order.job_id}/"
    admin_client.post(
        f"/work-orders/{work_order.pk}/notes/",
        {"text": "Glue-up done, finishing Friday.", "shared": "1"},
    )
    wos.add_note(work_order, text="Customer was rude on the phone.", user=admin)
    page = signed_in.get(job_url).content.decode()
    assert "Glue-up done, finishing Friday." in page
    assert "Customer was rude" not in page
    staff_page = admin_client.get(f"/work-orders/{work_order.pk}/?tab=notes").content.decode()
    assert "shared with the customer" in staff_page
    shared = work_order.notes.get(shared_with_customer=True)
    admin_client.post(f"/work-orders/{work_order.pk}/notes/{shared.pk}/share/", {"shared": "0"})
    assert "Glue-up done" not in signed_in.get(job_url).content.decode()
    # Another customer's shared note is out of reach.
    other = Customer.objects.create(name="Linus", email="linus@example.com")
    other_wo = _approved_work_order(other, admin)
    other_job = other_wo.job
    note = wos.add_note(other_wo, text="Theirs", user=admin, shared=True)
    assert signed_in.get(f"/portal/notes/{note.pk}/photo/").status_code == 404
    assert signed_in.get(f"/portal/jobs/{other_job.pk}/").status_code == 404


@pytest.mark.django_db
def test_sharing_a_note_is_offered_only_with_the_portal_on(admin_client, admin, customer):
    from trammel.core import modules

    modules.set_enabled("portal", False)
    wo = _approved_work_order(customer, admin)
    page = admin_client.get(f"/work-orders/{wo.pk}/?tab=notes").content.decode()
    assert "Share with the customer" not in page


@pytest.mark.django_db
def test_the_job_page_matches_the_edge_portal(
    signed_in,  # noqa: F811
    work_order,
    admin,  # noqa: F811
):
    """As the portal on Cloudflare shows a job (0.10.2): its due date, delivery or
    pickup, and a shared photo shown on the page itself."""
    import datetime as dt

    from django.core.files.uploadedfile import SimpleUploadedFile

    from tests.file_samples import _png

    job = work_order.job
    job.due_date, job.delivery_planned = dt.date(2026, 12, 1), dt.date(2026, 12, 5)
    job.save(update_fields=["due_date", "delivery_planned"])
    photo = SimpleUploadedFile("glue-up.png", _png(), content_type="image/png")
    note = wos.add_note(work_order, text="Glue-up done.", upload=photo, user=admin, shared=True)
    page = signed_in.get(f"/portal/jobs/{job.pk}/").content.decode()
    assert "<dt>Due</dt><dd>12/01/2026</dd>" in page
    assert "<dt>Delivery or pickup planned</dt><dd>12/05/2026</dd>" in page
    assert f'<img src="/portal/notes/{note.pk}/photo/"' in page
    assert signed_in.get(f"/portal/notes/{note.pk}/photo/").status_code == 200
