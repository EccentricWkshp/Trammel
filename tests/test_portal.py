"""The customer portal (FR-PRT): off until turned on, signing in by emailed code,
seeing only one's own records, and approving or declining quotes."""

import datetime as dt
import re
from decimal import Decimal

import pytest
from django.core import mail
from django.test import Client, override_settings
from django.utils import timezone

from tests.conftest import make_user
from trammel.agreements.models import CustomerAgreement
from trammel.catalog import services as catalog
from trammel.catalog.models import Product
from trammel.core import access, modules, tasks
from trammel.customers.models import Contact, Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.notifications.models import SmtpSettings
from trammel.portal import services as portal
from trammel.portal.models import SignInCode
from trammel.quotes import services as quotes
from trammel.quotes.forms import LINE_FORMS
from trammel.quotes.models import Approval

D = Decimal


@pytest.fixture
def smtp(setup_done):
    s = SmtpSettings.load()
    s.host, s.from_address, s.from_name = "smtp.example.com", "shop@example.com", "Test Shop"
    s.save()
    return s


@pytest.fixture
def portal_on(setup_done, smtp):
    modules.set_enabled("portal", True)


@pytest.fixture
def grace(customer, portal_on):
    portal.set_access(customer, True, user=None)
    return customer


def _sent_quote(customer, admin, title="Bookshelf"):
    job = jobs.create_job(Job(title=title, customer=customer))
    product = catalog.save_product(Product(name=f"{title} kit", kind="made", price=D("400")))
    revision = quotes.create_quote(job)
    form = LINE_FORMS["product"]({"product": str(product.pk), "quantity": "1"})
    assert form.is_valid(), form.errors
    quotes.save_line(form.build(revision))
    quotes.send(revision, actor=admin)
    revision.refresh_from_db()
    return revision


def _emailed_code(django_capture_on_commit_callbacks, client, email):
    sent = len(mail.outbox)
    response = client.post("/portal/sign-in/", {"email": email})
    # The worker looks the address up and queues the email, then sends it.
    with django_capture_on_commit_callbacks(execute=True):
        tasks.run_pending()
    tasks.run_pending()
    assert response.status_code == 302 and response["Location"] == "/portal/sign-in/code/"
    if len(mail.outbox) == sent:
        return None
    return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)


@pytest.fixture
def signed_in(grace, django_capture_on_commit_callbacks):
    client = Client()
    code = _emailed_code(django_capture_on_commit_callbacks, client, "grace@example.com")
    assert client.post("/portal/sign-in/code/", {"code": code})["Location"] == "/portal/"
    return client


@pytest.mark.django_db
def test_the_portal_is_off_until_turned_on_and_its_switch_is_on_the_system_page(admin_client, smtp):
    assert Client().get("/portal/sign-in/").status_code == 404
    page = admin_client.get("/settings/system/").content.decode()
    assert "Turn on the customer portal" in page
    admin_client.post("/settings/modules/portal/", {"enable": "1", "back": "system"})
    assert Client().get("/portal/sign-in/").status_code == 200
    page = admin_client.get("/settings/system/").content.decode()
    assert "Turn off the customer portal" in page
    response = admin_client.post("/settings/modules/portal/", {"enable": "0", "back": "system"})
    assert response["Location"] == "/settings/system/"
    assert Client().get("/portal/sign-in/").status_code == 404
    # Email keeps working with the portal off.
    assert SmtpSettings.load().is_configured


@pytest.mark.django_db
def test_the_server_setting_forces_it_off(portal_on, admin_client):
    with override_settings(PORTAL_OFF=True):
        assert not portal.is_on()
        assert Client().get("/portal/sign-in/").status_code == 404
        assert "PORTAL_OFF" in admin_client.get("/settings/system/").content.decode()
        with pytest.raises(Exception, match="server's configuration"):
            modules.set_enabled("portal", True)
    assert portal.is_on()


@pytest.mark.django_db
def test_signing_in_with_an_emailed_code(grace, django_capture_on_commit_callbacks):
    client = Client()
    # An address without access gets the same answer, and no email.
    assert _emailed_code(django_capture_on_commit_callbacks, client, "nobody@example.com") is None
    code = _emailed_code(django_capture_on_commit_callbacks, client, "Grace@Example.com ")
    assert code and "expires in 10 minutes" in mail.outbox[-1].body
    wrong = "000000" if code != "000000" else "111111"
    page = client.post("/portal/sign-in/code/", {"code": wrong}).content.decode()
    assert "didn&#x27;t work" in page
    assert client.post("/portal/sign-in/code/", {"code": code})["Location"] == "/portal/"
    assert "Hello, Grace Hopper" in client.get("/portal/").content.decode()
    # A code works once.
    client.post("/portal/sign-out/")
    client.session[portal.PENDING_KEY] = "grace@example.com"
    other = Client()
    session = other.session
    session[portal.PENDING_KEY] = "grace@example.com"
    session.save()
    assert other.post("/portal/sign-in/code/", {"code": code}).status_code == 200
    assert other.get("/portal/").status_code == 302


@pytest.mark.django_db
def test_a_contact_signs_in_under_their_own_name(grace, django_capture_on_commit_callbacks):
    Contact.objects.create(customer=grace, name="Ada Lovelace", email="ada@example.com")
    client = Client()
    code = _emailed_code(django_capture_on_commit_callbacks, client, "ada@example.com")
    client.post("/portal/sign-in/code/", {"code": code})
    page = client.get("/portal/").content.decode()
    assert "Hello, Ada Lovelace" in page and "Grace Hopper" in page


@pytest.mark.django_db
def test_codes_are_rate_limited_expire_and_die_after_wrong_tries(
    grace, django_capture_on_commit_callbacks
):
    client = Client()
    codes = [
        _emailed_code(django_capture_on_commit_callbacks, client, "grace@example.com")
        for _ in range(4)
    ]
    assert all(codes[:3]) and codes[3] is None  # three an address in 15 minutes
    # Asking again doesn't cancel a code already sent; each works once.
    assert portal.verify_code("grace@example.com", codes[0], "10.0.0.1")
    assert not portal.verify_code("grace@example.com", codes[0], "10.0.0.1")
    # Wrong tries count against every code still waiting, and use them up.
    wrong = next(c for c in ("999999", "888888", "777777") if c not in codes)
    for _ in range(SignInCode.MAX_TRIES - 1):
        portal.verify_code("grace@example.com", wrong, None)
    assert not portal.verify_code("grace@example.com", codes[2], None)
    assert not SignInCode.objects.filter(used_at__isnull=True, tries__lt=SignInCode.MAX_TRIES)

    from trammel.portal.models import Attempt

    SignInCode.objects.all().delete()
    Attempt.objects.all().delete()
    code = _emailed_code(django_capture_on_commit_callbacks, client, "grace@example.com")
    SignInCode.objects.update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    assert not portal.verify_code("grace@example.com", code, None)


@pytest.mark.django_db
def test_wrong_codes_for_one_address_are_capped_from_anywhere(
    grace, django_capture_on_commit_callbacks
):
    # Ten wrong codes in a day, each from a different address, and codes for
    # that address stop working — the real one included — and stop being sent.
    for n in range(portal.FAILURES_PER_ADDRESS):
        portal.verify_code("grace@example.com", "000000", f"198.51.100.{n}")
    client = Client()
    assert _emailed_code(django_capture_on_commit_callbacks, client, "grace@example.com") is None


@pytest.mark.django_db
def test_ipv6_limits_count_a_whole_slash_64():
    assert portal.ip_key("2001:db8:1:2:aaaa::1") == portal.ip_key("2001:db8:1:2:ffff::9")
    assert portal.ip_key("2001:db8:1:2::1") != portal.ip_key("2001:db8:1:3::1")
    assert portal.ip_key("203.0.113.9") == "203.0.113.9"


@pytest.mark.django_db
def test_customers_see_only_their_own_sent_records(signed_in, grace, admin):
    mine = _sent_quote(grace, admin)
    other = Customer.objects.create(name="Linus", email="linus@example.com")
    theirs = _sent_quote(other, admin, title="Desk")
    draft = quotes.create_quote(mine.quote.job)
    home = signed_in.get("/portal/").content.decode()
    assert "Bookshelf" in home and "Desk" not in home and "Ready for you to approve" in home
    assert signed_in.get(f"/portal/quotes/{mine.pk}/").status_code == 200
    assert signed_in.get(f"/portal/quotes/{mine.pk}/document/").status_code == 200
    for path in (
        f"/portal/quotes/{theirs.pk}/",
        f"/portal/quotes/{theirs.pk}/pdf/",
        f"/portal/jobs/{theirs.quote.job_id}/",
        f"/portal/quotes/{draft.pk}/",
    ):
        assert signed_in.get(path).status_code == 404, path
    # A signed-out visitor is sent to sign in.
    assert Client().get(f"/portal/quotes/{mine.pk}/")["Location"] == "/portal/sign-in/"


@pytest.mark.django_db
def test_approving_in_the_portal_records_who_when_and_the_accepted_terms(signed_in, grace, admin):
    # With signature capture off, the customer accepts by typing and ticking
    # (signing is in test_signatures.py).
    from trammel.agreements.models import AgreementSettings

    settings_ = AgreementSettings.load()
    settings_.signature_enabled = False
    settings_.save()
    revision = _sent_quote(grace, admin)
    page = signed_in.get(f"/portal/quotes/{revision.pk}/").content.decode()
    assert "Customer agreement" in page and "I have read and accept" in page
    assert "data-signature" not in page
    terms = re.search(r'name="terms" value="([0-9a-f]+)"', page).group(1)
    # Without ticking the box, nothing is approved.
    signed_in.post(
        f"/portal/quotes/{revision.pk}/respond/",
        {"answer": "approve", "name": "Grace Hopper", "terms": terms},
    )
    revision.refresh_from_db()
    assert revision.status == "sent"
    signed_in.post(
        f"/portal/quotes/{revision.pk}/respond/",
        {"answer": "approve", "name": "Grace Hopper", "terms": terms, "accept": "1"},
        REMOTE_ADDR="203.0.113.7",
    )
    revision.refresh_from_db()
    assert revision.status == "approved"
    approval = Approval.objects.get(revision=revision)
    assert approval.method == "portal" and approval.approved_by_name == "Grace Hopper"
    agreement = CustomerAgreement.objects.get(job=revision.quote.job, status="accepted")
    assert agreement.method == "click" and agreement.evidence_ip == "203.0.113.7"
    assert approval.agreement_text == agreement.text
    revision.quote.job.refresh_from_db()
    assert revision.quote.job.status == "approved"
    assert (
        "Approved by Grace Hopper"
        in signed_in.get(f"/portal/quotes/{revision.pk}/").content.decode()
    )


@pytest.mark.django_db
def test_terms_that_changed_since_the_page_opened_must_be_read_again(signed_in, grace, admin):
    revision = _sent_quote(grace, admin)
    response = signed_in.post(
        f"/portal/quotes/{revision.pk}/respond/",
        {"answer": "approve", "name": "Grace", "terms": "0" * 64, "accept": "1"},
        follow=True,
    )
    assert "terms have changed" in response.content.decode()
    revision.refresh_from_db()
    assert revision.status == "sent"


@pytest.mark.django_db
def test_declining_with_a_comment(signed_in, grace, admin):
    revision = _sent_quote(grace, admin)
    signed_in.post(
        f"/portal/quotes/{revision.pk}/respond/",
        {"answer": "decline", "comment": "Too pricey for now."},
    )
    revision.refresh_from_db()
    assert revision.status == "declined"


@pytest.mark.django_db
def test_turning_off_access_or_the_portal_signs_the_customer_out(signed_in, grace, admin):
    assert signed_in.get("/portal/").status_code == 200
    portal.set_access(grace, False, user=admin)
    assert signed_in.get("/portal/")["Location"] == "/portal/sign-in/"
    portal.set_access(grace, True, user=admin)
    assert signed_in.get("/portal/").status_code == 302  # signed out for good
    modules.set_enabled("portal", False)
    assert signed_in.get("/portal/").status_code == 404


@pytest.mark.django_db
def test_staff_turn_on_access_from_the_customer_page(admin_client, customer, portal_on):
    page = admin_client.get(f"/customers/{customer.pk}/").content.decode()
    assert "Customer portal" in page and "Turn on portal access" in page
    admin_client.post(f"/portal/access/{customer.pk}/", {"enabled": "1"})
    assert portal.has_access(customer)
    page = admin_client.get(f"/customers/{customer.pk}/").content.decode()
    assert "grace@example.com" in page and "Turn off portal access" in page

    viewer = make_user("v@example.com", "customers.view")
    client = Client()
    client.force_login(viewer)
    assert client.post(f"/portal/access/{customer.pk}/", {"enabled": "0"}).status_code == 403
    assert "Turn off portal access" not in client.get(f"/customers/{customer.pk}/").content.decode()


@pytest.mark.django_db
def test_customer_only_permissions_are_never_held_by_staff(admin):
    from trammel.accounts.forms import permission_choices

    offered = {code for _, perms in permission_choices() for code, _ in perms}
    assert "quotes.respond" not in offered and "portal.manage" in offered
    assert not access.has_perm(access.as_actor(admin), "quotes.respond")


@pytest.mark.django_db
def test_quote_emails_link_to_the_portal(
    admin_client, grace, admin, django_capture_on_commit_callbacks
):
    revision = _sent_quote(grace, admin)
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(f"/settings/email/quote/{revision.pk}/")
    tasks.run_pending()
    assert f"/portal/quotes/{revision.pk}/" in mail.outbox[-1].body
    portal.set_access(grace, False, user=admin)
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(f"/settings/email/quote/{revision.pk}/")
    tasks.run_pending()
    assert "/portal/" not in mail.outbox[-1].body


@pytest.mark.django_db(transaction=True)
def test_guesses_at_once_never_get_more_than_five_tries(grace, monkeypatch):
    """SEC-06 (0.10.5): ten guesses at the same moment are compared against a
    code no more than its five tries; the rest find it used up."""
    import concurrent.futures
    import datetime as dt
    import threading

    from django.db import connections

    from trammel.portal.models import SignInCode

    SignInCode.objects.create(
        email=grace.email,
        code_hash=SignInCode.hash(grace.email, "987654"),
        expires_at=timezone.now() + dt.timedelta(minutes=5),
    )
    real = portal._claim_tries
    ready = threading.Barrier(10)
    compared = []

    def claim(email):
        ready.wait(timeout=15)
        rows = real(email)
        compared.append(len(rows))
        return rows

    monkeypatch.setattr(portal, "_claim_tries", claim)

    def guess(n):
        try:
            return portal.verify_code(grace.email, f"{n:06d}", f"198.51.100.{n + 1}")
        finally:
            connections.close_all()

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(guess, range(10)))
    assert not any(results)
    assert sum(compared) == SignInCode.MAX_TRIES
    assert SignInCode.objects.get(email=grace.email).tries == SignInCode.MAX_TRIES
    # Used up: even the right code is refused now.
    monkeypatch.setattr(portal, "_claim_tries", real)
    assert portal.verify_code(grace.email, "987654", "198.51.100.200") == []
