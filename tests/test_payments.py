"""Online payments with Stripe (0.10.0): a payment link for each open invoice,
kept to its balance; payments read back from Stripe and recorded once; and the
Pay button in both portals. Stripe is a fake that answers as its API does."""

import datetime as dt
import json
import time
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from tests.test_billing import taxes  # noqa: F401
from tests.test_edge import live, worker  # noqa: F401
from tests.test_portal import grace, portal_on, signed_in, smtp  # noqa: F401
from trammel.accounts.models import ApiKey
from trammel.billing import services as billing
from trammel.billing.models import Invoice, Payment
from trammel.core import modules, secrets
from trammel.core.models import AuditEvent, BusinessProfile, SystemSettings
from trammel.edge import sync
from trammel.notifications.models import EmailLog
from trammel.payments import services, stripe
from trammel.payments.models import OnlinePayment, PayLink, StripeSettings

D = Decimal


class FakeStripe:
    """Stripe's API, as far as Trammel uses it."""

    def __init__(self):
        self.calls = []
        self.links = {}
        self.sessions = {}
        self.refuse = None

    def call(self, method, path, data=None, *, key=None):
        self.calls.append((method, path, data))
        if self.refuse:
            error = stripe.StripeError(self.refuse)
            error.status = 401
            raise error
        if path == "/payment_links" and method == "POST":
            n = len(self.links) + 1
            link = {"id": f"plink_{n}", "url": f"https://buy.stripe.com/test_{n}", "active": True}
            self.links[link["id"]] = {**link, "data": data}
            return link
        if path.startswith("/payment_links/") and method == "POST":
            self.links[path.rsplit("/", 1)[1]]["active"] = data["active"]
            return {}
        if path == "/payment_links":
            return {"data": []}
        if path == "/checkout/sessions":
            since = data["created"]["gte"]
            rows = [s for s in self.sessions.values() if s["created"] >= since]
            return {"data": rows, "has_more": False}
        if path.startswith("/checkout/sessions/"):
            return self.sessions[path.rsplit("/", 1)[1]]
        raise AssertionError(f"unexpected {method} {path}")

    def paid(self, link_id, invoice, amount, *, status="paid", n=None):
        """A customer paying through a link."""
        sid = f"cs_test_{n or len(self.sessions) + 1}"
        self.sessions[sid] = {
            "id": sid,
            "payment_link": link_id,
            "metadata": {"trammel_invoice": str(invoice.pk)},
            "amount_total": amount,
            "currency": "usd",
            "payment_status": status,
            "payment_intent": f"pi_{sid}",
            "customer_details": {"email": "grace@example.com"},
            "created": int(time.time()),
            "livemode": False,
        }
        return sid


@pytest.fixture
def fake(monkeypatch, setup_done):
    fake = FakeStripe()
    monkeypatch.setattr(stripe, "call", fake.call)
    return fake


@pytest.fixture
def paying(fake):
    modules.set_enabled("payments", True)
    secrets.put(stripe.KEY, "rk_test_abc")
    row = StripeSettings.load()
    row.enabled = True
    row.save()
    return fake


def _sent(subject):
    """The email with this subject, once the worker has sent what's queued."""
    from django.core import mail

    from trammel.core import tasks

    tasks.run_pending()
    return next(m for m in mail.outbox if m.subject.startswith(subject))


def _issued(customer, admin, amount="90"):
    draft = billing.create(customer, kind=Invoice.Kind.DIRECT_SALE)
    billing.add_line(draft, description="Cutting board", quantity=1, unit_price=D(amount))
    return billing.issue(draft, actor=admin)


@pytest.mark.django_db
def test_a_link_follows_the_invoices_balance(paying, taxes, customer, admin):  # noqa: F811
    invoice = _issued(customer, admin)  # 90 + 10.3% = 99.27
    assert services.keep_links() == 1
    made = paying.links["plink_1"]["data"]
    assert made["line_items"][0]["price_data"] == {
        "currency": "usd",
        "unit_amount": 9927,
        "product_data": {"name": f"{invoice.get_kind_display()} {invoice.number}"},
    }
    assert made["restrictions"] == {"completed_sessions": {"limit": 1}}
    assert made["metadata"]["trammel_invoice"] == str(invoice.pk)
    assert made["after_completion"]["type"] == "hosted_confirmation"  # no portal
    assert billing.pay_url(invoice) == "https://buy.stripe.com/test_1"
    assert billing.document_context(invoice)["invoice"]["pay_url"] == billing.pay_url(invoice)
    assert services.keep_links() == 0  # nothing changed

    # Part paid by check: a link for the rest; the old one is turned off.
    billing.record_payment(
        customer,
        amount=D("50"),
        received_on=timezone.localdate(),
        method="check",
        allocations={invoice: D("50")},
    )
    invoice.refresh_from_db()
    assert services.keep_links() == 1
    assert paying.links["plink_1"]["active"] is False
    assert paying.links["plink_2"]["data"]["line_items"][0]["price_data"]["unit_amount"] == 4927
    assert billing.pay_url(invoice) == "https://buy.stripe.com/test_2"

    # Paid off: no link.
    billing.record_payment(
        customer,
        amount=invoice.balance,
        received_on=timezone.localdate(),
        method="cash",
        allocations={invoice: invoice.balance},
    )
    invoice.refresh_from_db()
    services.keep_links()
    assert paying.links["plink_2"]["active"] is False and billing.pay_url(invoice) == ""

    # Voided: no link either.
    other = _issued(customer, admin, "20")
    services.keep_links()
    assert billing.pay_url(other)
    billing.void(other, actor=admin, reason="Wrong customer")
    services.keep_links()
    other.refresh_from_db()
    assert billing.pay_url(other) == "" and not PayLink.objects.filter(active=True).exists()


@pytest.mark.django_db
def test_a_balance_change_brings_a_new_link_at_once(
    paying,
    taxes,  # noqa: F811
    customer,
    admin,
    django_capture_on_commit_callbacks,  # noqa: F811
):
    from trammel.core.models import BackgroundTask

    with django_capture_on_commit_callbacks(execute=True):
        invoice = _issued(customer, admin)
    queued = BackgroundTask.objects.filter(name="payments.keep_links")
    assert queued.get().payload == {"invoice": str(invoice.pk)}
    queued.delete()
    with django_capture_on_commit_callbacks(execute=True):
        billing.record_payment(
            customer,
            amount=D("10"),
            received_on=timezone.localdate(),
            method="cash",
            allocations={invoice: D("10")},
        )
    assert queued.exists()


@pytest.mark.django_db
def test_the_link_comes_back_to_the_portal(paying, taxes, grace, admin, monkeypatch):  # noqa: F811
    invoice = _issued(grace, admin)
    services.keep_links()
    back = paying.links["plink_1"]["data"]["after_completion"]
    assert back["type"] == "redirect"
    assert back["redirect"]["url"].endswith(
        f"/portal/invoices/{invoice.pk}/?notice=payment-received"
    )
    # The portal moves to Cloudflare: a new link that comes back there.
    from trammel.portal import services as portal

    monkeypatch.setattr(portal, "served_elsewhere", lambda: "https://portal.example.com")
    assert services.keep_links() == 1
    assert paying.links["plink_2"]["data"]["after_completion"]["redirect"]["url"].startswith(
        "https://portal.example.com/portal/invoices/"
    )


@pytest.mark.django_db
def test_a_payment_is_recorded_once(
    paying,
    taxes,  # noqa: F811
    customer,
    admin,
    smtp,  # noqa: F811
    django_capture_on_commit_callbacks,
):
    invoice = _issued(customer, admin)
    services.keep_links()
    paying.paid("plink_1", invoice, 9927)
    with django_capture_on_commit_callbacks(execute=True):
        assert services.run_check() == {"recorded": 1, "links_made": 0}
    invoice.refresh_from_db()
    assert invoice.status == "paid" and invoice.balance == 0
    payment = Payment.objects.get()
    assert payment.method == "stripe" and payment.amount == D("99.27")
    assert payment.reference == "pi_cs_test_1" and "cs_test_1" in payment.notes
    online = OnlinePayment.objects.get()
    assert online.status == "recorded" and online.payment == payment and online.test_mode
    assert not PayLink.objects.filter(active=True).exists()
    staff = _sent("Paid online")
    assert "paid $99.27 online" in staff.body and invoice.number in staff.subject
    assert EmailLog.objects.filter(event="payment_recorded").exists()  # the receipt
    # Read again: nothing more.
    assert services.run_check() == {"recorded": 0, "links_made": 0}
    assert Payment.objects.count() == 1
    row = StripeSettings.load()
    assert row.last_check_at and row.checked_through and not row.last_error


@pytest.mark.django_db
def test_a_bank_payment_waits_until_its_through(paying, taxes, customer, admin):  # noqa: F811
    invoice = _issued(customer, admin)
    services.keep_links()
    sid = paying.paid("plink_1", invoice, 9927, status="unpaid")
    services.run_check()
    assert OnlinePayment.objects.get().status == "waiting" and not Payment.objects.exists()
    # Meanwhile no link, so it can't be paid twice.
    assert billing.pay_url(invoice) == "" and not PayLink.objects.filter(active=True).exists()
    paying.sessions[sid]["payment_status"] = "paid"
    assert services.run_check()["recorded"] == 1
    invoice.refresh_from_db()
    assert invoice.status == "paid"


@pytest.mark.django_db
def test_paid_after_a_void_is_held_as_credit(
    paying,
    taxes,  # noqa: F811
    customer,
    admin,
    smtp,  # noqa: F811
    django_capture_on_commit_callbacks,
):
    invoice = _issued(customer, admin)
    services.keep_links()
    billing.void(invoice, actor=admin, reason="Duplicate")
    paying.paid("plink_1", invoice, 9927)
    with django_capture_on_commit_callbacks(execute=True):
        services.run_check()
    assert billing.customer_credit(customer) == D("99.27")
    assert "held as credit" in _sent("Paid online").body


@pytest.mark.django_db
def test_nothing_happens_unless_its_on(paying, taxes, customer, admin):  # noqa: F811
    _issued(customer, admin)
    SystemSettings.objects.update_or_create(pk=1, defaults={"offline_mode": True})
    assert services.run_check() == {} and not paying.calls
    SystemSettings.objects.filter(pk=1).update(offline_mode=False)
    secrets.clear(stripe.KEY)
    assert services.keep_links() == 0 and not paying.calls
    secrets.put(stripe.KEY, "rk_test_abc")
    modules.set_enabled("payments", False)
    assert services.run_check() == {} and not paying.calls
    modules.set_enabled("payments", True)
    paying.refuse = "Stripe didn't accept the secret key."
    assert "didn't accept" in services.run_check()["error"]
    assert "didn't accept" in StripeSettings.load().last_error


def test_amounts_in_minor_units():
    assert services.to_minor(D("99.27"), "USD") == 9927
    assert services.to_minor(D("1500"), "JPY") == 1500
    assert services.from_minor(9927, "USD") == D("99.27")
    assert services.from_minor(1500, "JPY") == D("1500")
    assert stripe._form({"a": [{"b": {"c": 1}}], "d": True}) == [("a[0][b][c]", "1"), ("d", "true")]
    assert stripe.test_mode("rk_test_x") and not stripe.test_mode("sk_live_x")


@pytest.mark.django_db
def test_customers_see_a_pay_button(paying, taxes, grace, admin, signed_in):  # noqa: F811
    invoice = _issued(grace, admin)
    services.keep_links()
    page = signed_in.get(f"/portal/invoices/{invoice.pk}/").content.decode()
    assert 'href="https://buy.stripe.com/test_1"' in page and "Pay $99.27 online" in page
    home = signed_in.get("/portal/").content.decode()
    assert "https://buy.stripe.com/test_1" in home
    back = signed_in.get(f"/portal/invoices/{invoice.pk}/?notice=payment-received")
    assert "Your payment is being confirmed" in back.content.decode()
    assert (
        "Your payment"
        not in signed_in.get(f"/portal/invoices/{invoice.pk}/?notice=<b>x</b>").content.decode()
    )


@pytest.mark.django_db
def test_the_pay_link_is_published_to_cloudflare(paying, taxes, live, grace, admin):  # noqa: F811
    invoice = _issued(grace, admin)
    services.keep_links()
    sync.run()
    assert live.of("invoice")[str(invoice.pk)]["data"]["pay_url"] == "https://buy.stripe.com/test_1"
    services.make_link(invoice)
    sync.run()
    assert live.of("invoice")[str(invoice.pk)]["data"]["pay_url"] == "https://buy.stripe.com/test_2"


@pytest.mark.django_db
def test_the_settings_page_and_the_invoice_panel(paying, taxes, customer, admin, admin_client):  # noqa: F811
    secrets.clear(stripe.KEY)
    page = admin_client.get("/settings/online-payments/").content.decode()
    assert "Payment Links: Write" in page and "rk_test_abc" not in page
    response = admin_client.post(
        "/settings/online-payments/", {"stripe-enabled": "on", "stripe-stripe_key": "pk_live_x"}
    )
    assert "it starts with sk_ or rk_" in response.content.decode()
    response = admin_client.post(
        "/settings/online-payments/", {"stripe-enabled": "on", "stripe-stripe_key": "rk_test_new"}
    )
    assert response.status_code == 302 and secrets.get(stripe.KEY) == "rk_test_new"
    assert "rk_test_new" not in admin_client.get("/settings/online-payments/").content.decode()
    assert AuditEvent.objects.filter(summary__contains="Stripe secret key").exists()
    admin_client.post("/settings/online-payments/test/")
    assert ("GET", "/payment_links", {"limit": 1}) in paying.calls

    invoice = _issued(customer, admin)
    admin_client.post("/settings/online-payments/check/")
    panel = admin_client.get(f"/invoices/{invoice.pk}/").content.decode()
    assert "Online payment" in panel and "https://buy.stripe.com/test_1" in panel
    admin_client.post(f"/settings/online-payments/invoices/{invoice.pk}/link/")
    assert billing.pay_url(invoice) == "https://buy.stripe.com/test_2"
    assert AuditEvent.objects.filter(action="payments.new_link").exists()


@pytest.mark.django_db
def test_online_payments_over_the_api(paying, taxes, customer, admin):  # noqa: F811
    issued = ApiKey.issue(admin, "Test", ["payments.manage", "billing.view"])
    token = issued[1] if isinstance(issued, tuple) else issued
    client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")

    def api(method, path, body=None, expect=200):
        response = getattr(client, method.lower())(
            "/api/v1/payments" + path,
            data=json.dumps(body) if body is not None else None,
            content_type="application/json",
        )
        assert response.status_code == expect, response.content[:300]
        return response.json()

    shown = api("GET", "/settings")
    assert shown["active"] and shown["test_mode"] and shown["key_set"]
    assert "rk_test" not in json.dumps(shown)
    api("PATCH", "/settings", {"stripe_key": "pk_x"}, expect=422)
    assert api("PATCH", "/settings", {"enabled": False})["active"] is False
    api("PATCH", "/settings", {"enabled": True})
    invoice = _issued(customer, admin)
    assert api("POST", "/check")["links_made"] == 1
    found = api("GET", f"/invoices/{invoice.pk}")
    assert found["link"]["url"] == "https://buy.stripe.com/test_1" and found["payments"] == []
    remade = api("POST", f"/invoices/{invoice.pk}/link")
    assert remade["link"]["url"] == "https://buy.stripe.com/test_2"
    paying.paid("plink_2", invoice, 9927)
    api("POST", "/check")
    found = api("GET", f"/invoices/{invoice.pk}")
    assert found["link"] is None and found["payments"][0]["status"] == "recorded"
    # Without the permission: refused.
    other = ApiKey.issue(admin, "Other", ["billing.view"])
    token = other[1] if isinstance(other, tuple) else other
    response = Client(HTTP_AUTHORIZATION=f"Bearer {token}").get("/api/v1/payments/settings")
    assert response.status_code == 403
    assert dt  # (imported for the times above)
    assert BusinessProfile.load().currency == "USD"
