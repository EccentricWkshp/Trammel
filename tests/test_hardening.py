"""Hardening for internet exposure (Phase 3): sign-in lockouts that a stranger
can't turn against someone, the setup page, template limits, PDF fetching,
secrets kept out of the task queue, file sharing and authenticator changes."""

import types

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from tests.conftest import make_user
from trammel.accounts import mfa
from trammel.core import tasks, templating
from trammel.core.models import BackgroundTask
from trammel.files import services as files
from trammel.files.models import Attachment
from trammel.notifications.models import SmtpSettings

PASSWORD = "correct horse battery"


def _login(email, password, ip):
    return Client(REMOTE_ADDR=ip).post("/accounts/login/", {"email": email, "password": password})


@pytest.mark.django_db
@override_settings(LOGIN_MAX_FAILURES=3)
def test_a_guesser_elsewhere_cannot_lock_someone_out(admin):
    for _ in range(5):
        _login(admin.email, "wrong", "198.51.100.9")
    assert "Too many" in _login(admin.email, PASSWORD, "198.51.100.9").content.decode()
    # The real person, from their own address, still gets in.
    assert _login(admin.email, PASSWORD, "203.0.113.20").status_code == 302


@pytest.mark.django_db
@override_settings(LOGIN_MAX_FAILURES=3, LOGIN_MAX_FAILURES_PER_ACCOUNT=6)
def test_guessing_spread_over_many_addresses_still_locks_the_account(admin):
    for n in range(6):
        _login(admin.email, "wrong", f"198.51.100.{n}")
    assert "Too many" in _login(admin.email, PASSWORD, "203.0.113.20").content.decode()


@pytest.mark.django_db
@override_settings(LOGIN_MAX_FAILURES=3)
def test_a_locked_password_doesnt_stop_an_emailed_link(admin, django_capture_on_commit_callbacks):
    from trammel.core.models import AuditEvent

    smtp = SmtpSettings.load()
    smtp.host, smtp.from_address = "smtp.example.com", "shop@example.com"
    smtp.save()
    client = Client(REMOTE_ADDR="203.0.113.20")
    for _ in range(6):
        client.post("/accounts/login/", {"email": admin.email, "password": "wrong"})
    # Refusals are logged once per window, not once per try.
    assert AuditEvent.objects.filter(action="auth.locked").count() == 1
    with django_capture_on_commit_callbacks(execute=True):
        client.post("/accounts/email-link/", {"email": admin.email})
    tasks.run_pending()
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_sign_in_links_and_codes_are_cleared_from_the_queue_once_sent(
    admin, django_capture_on_commit_callbacks
):
    from trammel.notifications.services import notify

    smtp = SmtpSettings.load()
    smtp.host, smtp.from_address = "smtp.example.com", "shop@example.com"
    smtp.save()
    with django_capture_on_commit_callbacks(execute=True):
        notify("magic_link", to=[admin.email], context={"link": "https://x/secret", "minutes": 15})
        notify("backup_failed", context={"when": "today", "error": "disk full"})
    tasks.run_pending()
    assert "https://x/secret" in mail.outbox[0].body
    payloads = [t.payload for t in BackgroundTask.objects.filter(name="notifications.send")]
    assert {"secret": True} in payloads
    assert not any("https://x/secret" in str(p) for p in payloads)
    assert any("disk full" in str(p) for p in payloads)


@pytest.mark.django_db
def test_setup_from_outside_the_local_network_needs_the_token(client):
    from tests.test_setup import SETUP

    outside = Client(REMOTE_ADDR="198.51.100.4")
    assert outside.get("/setup/").status_code == 403
    assert outside.post("/setup/", SETUP).status_code == 403
    with override_settings(SETUP_TOKEN="a-long-random-token"):
        assert outside.get("/setup/").status_code == 200
    assert Client(REMOTE_ADDR="192.168.1.20").get("/setup/").status_code == 200


@pytest.mark.parametrize(
    "source",
    ["{{ 'a' * 10**9 }}", "{{ 9 ** 9 ** 9 }}", "{{ [1] * 10**7 }}", "{{ 10 ** 30 * 10 ** 30 }}"],
)
def test_templates_cant_make_huge_numbers_or_text(source):
    with pytest.raises(templating.TemplateProblem):
        templating.render(source, {}, html=False)


def test_ordinary_arithmetic_in_templates_still_works():
    assert templating.render("{{ 3 * 4 }} {{ 2 ** 10 }} {{ '-' * 20 }}", {}, html=False) == (
        "12 1024 " + "-" * 20
    )


def test_pdfs_read_only_embedded_data(monkeypatch):
    made = {}

    class FakeFetcher:
        def __init__(self, **kwargs):
            made.update(kwargs)

    class FakeHTML:
        def __init__(self, string, url_fetcher):
            made["fetcher"] = url_fetcher

        def write_pdf(self):
            return b"%PDF"

    fake = types.SimpleNamespace(URLFetcher=FakeFetcher, HTML=FakeHTML)
    monkeypatch.setattr(templating, "_import_weasyprint", lambda: fake)
    assert templating.render_pdf("<p>x</p>") == b"%PDF"
    assert made["allowed_protocols"] == {"data"} and made["allow_redirects"] is False


@pytest.mark.django_db
def test_sharing_a_file_needs_the_right_to_see_its_record(admin, customer):
    files.upload(
        SimpleUploadedFile("plan.pdf", b"%PDF-1.4 plan"),
        name="plan.pdf",
        attach_to=customer,
        user=admin,
    )
    link = Attachment.objects.get()
    uploader = make_user("u@example.com", "files.upload", "files.view")
    client = Client()
    client.force_login(uploader)
    assert client.post(f"/files/link/{link.pk}/share/").status_code == 403
    link.refresh_from_db()
    assert not link.shared_with_customer


@pytest.mark.django_db
def test_replacing_an_authenticator_needs_a_current_code(admin_client, admin):
    old_secret = mfa.new_secret()
    mfa.enable(admin, old_secret)
    admin_client.get("/accounts/two-factor/setup/")
    new_secret = admin_client.session["mfa_new_secret"]
    page = admin_client.post(
        "/accounts/two-factor/setup/", {"code": mfa.code_for(new_secret)}
    ).content.decode()
    assert "current code" in page
    admin.refresh_from_db()
    assert mfa.enabled(admin)


@pytest.mark.django_db
def test_strangers_dont_see_the_version(client, setup_done):
    from django.conf import settings

    assert settings.APP_VERSION not in client.get("/accounts/login/").content.decode()
    assert "version" not in client.get("/healthz").json()


@pytest.mark.django_db
def test_admins_are_urged_to_require_two_factor_sign_in(admin_client):
    from trammel.core.models import SystemSettings

    reminder = "Action item: require two-factor sign-in"
    assert reminder in admin_client.get("/").content.decode()
    assert reminder in admin_client.get("/settings/system/").content.decode()
    assert reminder not in admin_client.get("/customers/").content.decode()
    system = SystemSettings.load()
    system.require_mfa_for_admins = True
    system.save()
    assert reminder not in admin_client.get("/settings/system/").content.decode()


@pytest.mark.django_db
def test_background_jobs_write_times_in_the_shops_time_zone(setup_done, monkeypatch):
    import datetime as dt

    from trammel.core import formats
    from trammel.core.models import BusinessProfile

    business = BusinessProfile.load()
    business.timezone = "America/Los_Angeles"
    business.save()
    seen = []
    at = dt.datetime(2026, 11, 11, 2, 28, tzinfo=dt.UTC)
    monkeypatch.setitem(
        tasks._handlers, "test.clock", lambda payload: seen.append(formats.show(at, "dt"))
    )
    tasks.enqueue("test.clock")
    tasks.run_pending()
    assert seen == ["11/10/2026 6:28 PM"]


@pytest.mark.django_db
@override_settings(ALLOWED_HOSTS=["trammel.example.com"], SECURE_SSL_REDIRECT=True)
def test_the_container_health_check_gets_through(setup_done):
    # The health check calls 127.0.0.1 over plain HTTP: not an allowed host name,
    # and not HTTPS.
    response = Client(HTTP_HOST="127.0.0.1:8080").get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert Client(HTTP_HOST="127.0.0.1:8080").get("/accounts/login/").status_code == 400
