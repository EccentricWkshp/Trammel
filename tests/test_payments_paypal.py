"""PayPal (0.10.1): each open invoice gets one PayPal invoice, numbered exactly
as in Trammel and sent to nobody, kept to the balance by updating it, and
cancelled once it's no longer owed; payments read back and recorded once. PayPal
is a fake that answers as its Invoicing API (v2) does."""

import datetime as dt
import json
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from tests.test_billing import taxes  # noqa: F401
from tests.test_edge import live, worker  # noqa: F401
from tests.test_payments import FakeStripe, _issued, _sent
from tests.test_portal import grace, portal_on, signed_in, smtp  # noqa: F401
from trammel.accounts.models import ApiKey
from trammel.billing import services as billing
from trammel.billing.models import Payment
from trammel.core import modules, secrets
from trammel.edge import sync
from trammel.payments import paypal, services, stripe
from trammel.payments.models import OnlinePayment, PayLink, PayPalSettings, StripeSettings

D = Decimal


class FakePayPal:
    """PayPal's Invoicing API (v2), as far as Trammel uses it."""

    def __init__(self):
        self.calls = []
        self.invoices = {}
        self.taken_numbers = set()
        self.refuse_update = False
        self.refuse = None

    def _error(self, status, issue="", message="Refused"):
        error = paypal.PayPalError(f"PayPal answered {status}: {message}")
        error.status, error.issue = status, issue
        return error

    def call(self, method, path, data=None, *, query=None):
        self.calls.append((method, path, data, query))
        if self.refuse:
            raise self._error(401, message=self.refuse)
        parts = path.split("/")
        if path == "/v2/invoicing/invoices" and method == "GET":
            return {"items": []}
        if path == "/v2/invoicing/invoices" and method == "POST":
            number = data["detail"].get("invoice_number")
            if number in self.taken_numbers:
                raise self._error(422, "DUPLICATE_INVOICE_NUMBER")
            n = len(self.invoices) + 1
            invoice_id = f"INV2-TEST-{n:04d}"
            number = number or f"{n:04d}"  # PayPal's own sequence
            self.taken_numbers.add(number)
            self.invoices[invoice_id] = {
                "id": invoice_id,
                "status": "DRAFT",
                "detail": {
                    **data["detail"],
                    "invoice_number": number,
                    "metadata": {
                        "recipient_view_url": f"https://www.sandbox.paypal.com/invoice/p/#{invoice_id}"
                    },
                },
                "items": data["items"],
                "configuration": data["configuration"],
            }
            return dict(self.invoices[invoice_id])
        if path == "/v2/invoicing/search-invoices":
            start = dt.datetime.fromisoformat(
                data["payment_date_range"]["start"].replace("Z", "+00:00")
            )
            rows = [
                {"id": i["id"], "status": i["status"]}
                for i in self.invoices.values()
                if i["status"] in data["status"] and i.get("paid_at") and i["paid_at"] >= start
            ]
            return {"items": rows, "total_pages": 1}
        invoice = self.invoices[parts[4]]
        if method == "POST" and parts[-1] == "send":
            assert data == {"send_to_recipient": False, "send_to_invoicer": False}
            invoice["status"] = "UNPAID"
            return {"href": invoice["detail"]["metadata"]["recipient_view_url"]}
        if method == "POST" and parts[-1] == "cancel":
            assert data == {"send_to_recipient": False, "send_to_invoicer": False}
            if invoice["status"] in ("PAID", "PAYMENT_PENDING"):
                raise self._error(422, "INVOICE_ALREADY_PAID")
            invoice["status"] = "CANCELLED"
            return {}
        if method == "PUT":
            assert query == {"send_to_recipient": "false", "send_to_invoicer": "false"}
            if self.refuse_update:
                raise self._error(422, "CANNOT_UPDATE")
            assert data["detail"]["invoice_number"] == invoice["detail"]["invoice_number"]
            invoice["items"] = data["items"]
            return dict(invoice)
        if method == "GET":
            return json.loads(json.dumps(invoice, default=str))
        raise AssertionError(f"unexpected {method} {path}")

    def amount(self, invoice_id):
        return self.invoices[invoice_id]["items"][0]["unit_amount"]["value"]

    def pay(self, invoice_id, *, status="PAID"):
        """A customer paying the PayPal invoice."""
        invoice = self.invoices[invoice_id]
        value = self.amount(invoice_id)
        invoice["status"] = status
        invoice["paid_at"] = timezone.now()
        invoice["payments"] = {
            "paid_amount": {"currency_code": "USD", "value": value},
            "transactions": [
                {
                    "payment_id": f"TX-{invoice_id}",
                    "payment_date": timezone.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "method": "PAYPAL",
                    "amount": {"currency_code": "USD", "value": value},
                }
            ],
        }


@pytest.fixture
def pp(monkeypatch, setup_done):
    fake = FakePayPal()
    monkeypatch.setattr(paypal, "call", fake.call)
    monkeypatch.setattr(
        paypal, "signed_in", lambda: {"scopes": ["openid", paypal.INVOICING], "hours": 9}
    )
    modules.set_enabled("payments", True)
    secrets.put(paypal.SECRET_KEY, "pp-secret")
    PayPalSettings.objects.update_or_create(
        pk=1, defaults={"enabled": True, "sandbox": True, "client_id": "pp-client"}
    )
    return fake


@pytest.mark.django_db
def test_a_paypal_invoice_numbered_as_in_trammel(pp, taxes, customer, admin):  # noqa: F811
    invoice = _issued(customer, admin)  # 99.27
    assert services.keep_links() == 1
    made = pp.invoices["INV2-TEST-0001"]
    assert made["status"] == "UNPAID"  # sent, to nobody
    assert made["detail"]["invoice_number"] == invoice.number
    assert made["detail"]["reference"] == invoice.number
    assert made["detail"]["memo"] == f"trammel:{invoice.pk}"
    assert made["items"][0]["unit_amount"] == {"currency_code": "USD", "value": "99.27"}
    assert made["configuration"] == {
        "allow_tip": False,
        "partial_payment": {"allow_partial_payment": False},
    }
    link = PayLink.objects.get()
    assert link.provider == "paypal" and link.number == invoice.number and link.test_mode
    assert billing.pay_options(invoice) == [
        {
            "label": "Pay $99.27 with PayPal",
            "name": "PayPal",
            "url": "https://www.sandbox.paypal.com/invoice/p/#INV2-TEST-0001",
        }
    ]

    # Part paid by check: the same PayPal invoice, for the rest; no new number.
    billing.record_payment(
        customer,
        amount=D("50"),
        received_on=timezone.localdate(),
        method="check",
        allocations={invoice: D("50")},
    )
    invoice.refresh_from_db()
    assert services.keep_links() == 0
    assert len(pp.invoices) == 1 and pp.amount("INV2-TEST-0001") == "49.27"
    link.refresh_from_db()
    assert link.active and link.amount == D("49.27") and link.number == invoice.number
    assert billing.pay_url(invoice) == "https://www.sandbox.paypal.com/invoice/p/#INV2-TEST-0001"

    # Paid off another way: cancelled.
    billing.record_payment(
        customer,
        amount=invoice.balance,
        received_on=timezone.localdate(),
        method="cash",
        allocations={invoice: invoice.balance},
    )
    services.keep_links()
    assert pp.invoices["INV2-TEST-0001"]["status"] == "CANCELLED"
    assert not PayLink.objects.filter(active=True).exists()


@pytest.mark.django_db
def test_never_a_look_alike_of_a_trammel_number(pp, taxes, customer, admin):  # noqa: F811
    invoice = _issued(customer, admin)
    # The number is already used on the PayPal account (made there by hand).
    pp.taken_numbers.add(invoice.number)
    services.keep_links()
    made = pp.invoices["INV2-TEST-0001"]
    assert made["detail"]["invoice_number"] == "0001"  # PayPal's own sequence
    assert made["detail"]["reference"] == invoice.number
    assert made["items"][0]["name"].endswith(invoice.number)
    # An update PayPal refuses: a new PayPal invoice, PayPal-numbered, never "-2".
    pp.refuse_update = True
    billing.record_payment(
        customer,
        amount=D("10"),
        received_on=timezone.localdate(),
        method="cash",
        allocations={invoice: D("10")},
    )
    services.keep_links()
    assert pp.invoices["INV2-TEST-0001"]["status"] == "CANCELLED"
    numbers = [i["detail"]["invoice_number"] for i in pp.invoices.values()]
    assert not any(n.startswith(f"{invoice.number}-") for n in numbers)
    assert PayLink.objects.get(active=True).link_id == "INV2-TEST-0002"


@pytest.mark.django_db
def test_a_paypal_payment_is_recorded_once(
    pp,
    taxes,  # noqa: F811
    customer,
    admin,
    smtp,  # noqa: F811
    django_capture_on_commit_callbacks,
):
    invoice = _issued(customer, admin)
    services.keep_links()
    pp.pay("INV2-TEST-0001")
    with django_capture_on_commit_callbacks(execute=True):
        assert services.run_check() == {"recorded": 1, "links_made": 0}
    invoice.refresh_from_db()
    assert invoice.status == "paid"
    payment = Payment.objects.get()
    assert payment.method == "paypal" and payment.amount == D("99.27")
    assert payment.reference == "TX-INV2-TEST-0001" and "INV2-TEST-0001" in payment.notes
    online = OnlinePayment.objects.get()
    assert online.provider == "paypal" and online.status == "recorded"
    assert "Recorded as a PayPal payment" in _sent("Paid online").body
    assert services.run_check() == {"recorded": 0, "links_made": 0}
    assert Payment.objects.count() == 1 and PayPalSettings.load().checked_through


@pytest.mark.django_db
def test_paid_on_paypal_after_a_void_is_credit(pp, taxes, customer, admin):  # noqa: F811
    invoice = _issued(customer, admin)
    services.keep_links()
    pp.pay("INV2-TEST-0001")  # paid just before the void was seen
    billing.void(invoice, actor=admin, reason="Duplicate")
    services.run_check()
    assert billing.customer_credit(customer) == D("99.27")


@pytest.fixture
def both(pp, monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(stripe, "call", fake.call)
    secrets.put(stripe.KEY, "rk_test_abc")
    StripeSettings.objects.update_or_create(pk=1, defaults={"enabled": True})
    return fake


def _offered(invoice):
    invoice.refresh_from_db()
    return [o["name"] for o in billing.pay_options(invoice)]


@pytest.mark.django_db
def test_one_way_to_pay_at_a_time(both, pp, taxes, grace, admin, signed_in):  # noqa: F811
    """The primary unless the customer or the invoice says otherwise; the old page
    is closed before the new one is made, so it's never payable twice."""
    from trammel.payments.models import PaymentsSettings

    invoice = _issued(grace, admin)
    assert services.keep_links() == 1
    assert _offered(invoice) == ["Card"]  # Stripe, the primary
    page = signed_in.get(f"/portal/invoices/{invoice.pk}/").content.decode()
    assert "Pay $99.27 online" in page and "with PayPal" not in page
    assert services.choice_of(invoice)["why"] == "the shop's primary"

    # The customer prefers PayPal: Stripe's link closed first, then PayPal's made.
    services.choose_for_customer(grace, "paypal")
    services.keep_links()
    assert _offered(invoice) == ["PayPal"]
    assert both.links["plink_1"]["active"] is False
    assert PayLink.objects.filter(active=True).count() == 1
    order = [c[1] for c in both.calls if c[0] == "POST"] + [
        c[1] for c in pp.calls if c[0] == "POST"
    ]
    assert "/payment_links/plink_1" in order
    page = signed_in.get(f"/portal/invoices/{invoice.pk}/").content.decode()
    assert "Pay $99.27 with PayPal" in page and "Pay $99.27 online" not in page
    assert services.choice_of(invoice)["why"] == "the customer's choice"

    # This invoice by card after all: the invoice wins.
    services.choose_for_invoice(invoice, "stripe")
    services.keep_links()
    assert _offered(invoice) == ["Card"]
    assert pp.invoices["INV2-TEST-0001"]["status"] == "CANCELLED"
    assert services.choice_of(invoice)["why"] == "chosen for this invoice"

    # Back to the customer's way, then the primary changed: everything follows.
    services.choose_for_invoice(invoice, None)
    services.choose_for_customer(grace, None)
    PaymentsSettings.objects.update_or_create(pk=1, defaults={"primary": "paypal"})
    services.keep_links()
    assert _offered(invoice) == ["PayPal"]
    # The chosen one switched off: the other is used.
    PayPalSettings.objects.filter(pk=1).update(enabled=False)
    services.keep_links()
    assert _offered(invoice) == ["Card"] and PayLink.objects.filter(active=True).count() == 1


@pytest.mark.django_db
def test_no_switch_while_its_being_paid(both, pp, taxes, customer, admin):  # noqa: F811
    invoice = _issued(customer, admin)
    services.choose_for_invoice(invoice, "paypal")
    services.keep_links()
    pp.pay("INV2-TEST-0001", status="PAYMENT_PENDING")  # an eCheck, clearing
    services.choose_for_invoice(invoice, "stripe")
    assert services.keep_links() == 0  # no Stripe link meanwhile
    assert not both.links
    link = PayLink.objects.get(active=True)
    assert link.provider == "paypal"


@pytest.mark.django_db
def test_published_one_way_and_one_failing_spares_the_other(
    both,
    pp,
    taxes,  # noqa: F811
    grace,  # noqa: F811
    admin,
    live,  # noqa: F811
):
    by_card = _issued(grace, admin)
    by_paypal = _issued(grace, admin, "20")
    services.choose_for_invoice(by_paypal, "paypal")
    services.keep_links()
    sync.run()
    assert [o["name"] for o in live.of("invoice")[str(by_card.pk)]["data"]["pay"]] == ["Card"]
    assert [o["name"] for o in live.of("invoice")[str(by_paypal.pk)]["data"]["pay"]] == ["PayPal"]
    # One provider failing doesn't stop the other's check.
    pp.refuse = "PayPal didn't accept the client ID and secret."
    both.paid("plink_1", by_card, 9927)
    result = services.run_check()
    assert result["recorded"] == 1 and "PayPal didn't accept" in result["error"]
    assert "PayPal didn't accept" in PayPalSettings.load().last_error
    assert not StripeSettings.load().last_error


@pytest.mark.django_db
def test_choosing_from_the_pages_and_the_api(both, pp, taxes, customer, admin, admin_client):  # noqa: F811
    from trammel.payments.models import CustomerChoice, InvoiceChoice, PaymentsSettings

    invoice = _issued(customer, admin)
    page = admin_client.get(customer.get_absolute_url()).content.decode()
    assert "Offer their invoices through" in page
    admin_client.post(
        f"/settings/online-payments/customers/{customer.pk}/provider/", {"provider": "paypal"}
    )
    assert CustomerChoice.objects.get(customer=customer).provider == "paypal"
    page = admin_client.get(f"/invoices/{invoice.pk}/").content.decode()
    assert "Offer it through" in page and "the customer&#x27;s choice" in page
    admin_client.post(
        f"/settings/online-payments/invoices/{invoice.pk}/provider/", {"provider": "stripe"}
    )
    assert InvoiceChoice.objects.get(invoice=invoice).provider == "stripe"
    admin_client.post("/settings/online-payments/primary/", {"primary": "paypal"})
    assert PaymentsSettings.load().primary == "paypal"

    issued = ApiKey.issue(admin, "Test", ["payments.manage", "billing.view", "customers.view"])
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

    assert api("GET", "/settings")["primary"] == "paypal"
    assert api("PATCH", "/settings", {"primary": "stripe"})["primary"] == "stripe"
    assert api("GET", f"/customers/{customer.pk}")["chosen"] == "paypal"
    assert api("PUT", f"/customers/{customer.pk}", {"provider": None})["chosen"] == ""
    found = api("PUT", f"/invoices/{invoice.pk}", {"provider": "paypal"})
    assert found["provider"] == "paypal" and found["chosen"] == "paypal"
    assert found["why"] == "chosen for this invoice"
    api("PUT", f"/invoices/{invoice.pk}", {"provider": "bitcoin"}, expect=422)


@pytest.mark.django_db
def test_switching_paypal_off_cancels_its_invoices(pp, taxes, customer, admin):  # noqa: F811
    invoice = _issued(customer, admin)
    services.keep_links()
    PayPalSettings.objects.filter(pk=1).update(enabled=False)
    services.keep_links()
    assert pp.invoices["INV2-TEST-0001"]["status"] == "CANCELLED"
    assert billing.pay_options(invoice) == []


@pytest.mark.django_db
def test_signing_in_to_paypal(monkeypatch, setup_done):
    """A token from client credentials, kept until it runs out; a 401 gets a new one."""
    modules.set_enabled("payments", True)
    secrets.put(paypal.SECRET_KEY, "pp-secret")
    PayPalSettings.objects.update_or_create(pk=1, defaults={"client_id": "pp-client"})
    sent = []
    expired = {"now": False}

    def send(method, url, *, body=None, headers=None, signing_in=False):
        sent.append((method, url, headers.get("Authorization", "")))
        if url.endswith("/v1/oauth2/token"):
            # Invoicing asked for by name: PayPal's default sign-in can leave it out.
            assert body == (
                b"grant_type=client_credentials"
                b"&scope=https%3A%2F%2Furi.paypal.com%2Fservices%2Finvoicing"
            )
            assert url.startswith(paypal.SANDBOX)
            return {"access_token": f"tok{len(sent)}", "expires_in": 32400}
        if "tok1" in headers["Authorization"] and expired["now"]:
            expired["now"] = False
            error = paypal.PayPalError("PayPal answered 401: expired")
            error.status = 401
            raise error
        return {"items": []}

    monkeypatch.setattr(paypal, "_send", send)
    paypal.invoice("INV2-1")
    paypal.invoice("INV2-1")
    tokens = [s for s in sent if s[1].endswith("/v1/oauth2/token")]
    assert len(tokens) == 1  # kept
    expired["now"] = True
    paypal.invoice("INV2-1")  # the token expired: a new one, and tried again
    assert len([s for s in sent if s[1].endswith("/v1/oauth2/token")]) == 2


@pytest.mark.django_db
def test_the_test_says_why_paypal_refuses(monkeypatch, setup_done):
    """The test signs in afresh and reads what PayPal granted: Invoicing left out
    of the app's sign-in, or refused despite it (the account), with the debug ID."""
    modules.set_enabled("payments", True)
    secrets.put(paypal.SECRET_KEY, "pp-secret")
    PayPalSettings.objects.update_or_create(
        pk=1, defaults={"client_id": "AbCdEf-client-xyz123", "sandbox": True}
    )
    answer = {"scope": "openid https://uri.paypal.com/services/payments/payment", "refuse": False}
    signs_in = []

    def send(method, url, *, body=None, headers=None, signing_in=False):
        if url.endswith("/v1/oauth2/token"):
            signs_in.append(1)
            return {"access_token": "tok", "scope": answer["scope"], "expires_in": 32400}
        if answer["refuse"]:
            error = paypal.PayPalError("refused")
            error.status, error.detail, error.debug_id = 403, "Not authorized.", "f00dbabe"
            raise error
        return {"items": []}

    monkeypatch.setattr(paypal, "_send", send)
    with pytest.raises(paypal.PayPalError) as missing:
        paypal.check_credentials()
    text = str(missing.value)
    assert "hasn't given it Invoicing" in text and "Sandbox" in text and "…xyz123" in text

    answer["scope"] = paypal.INVOICING
    paypal.check_credentials()  # signed in afresh each time: the change shows at once
    assert len(signs_in) == 2

    answer["refuse"] = True
    with pytest.raises(paypal.PayPalError) as refused:
        paypal.check_credentials()
    text = str(refused.value)
    assert "(Not authorized.)" in text and "does include Invoicing" in text
    assert "Business" in text and "f00dbabe" in text


@pytest.mark.django_db
def test_paypal_refusing_the_invoicing_scope(monkeypatch, setup_done):
    """An app without Invoicing: PayPal refuses the scope when signing in."""
    import io
    import urllib.error

    modules.set_enabled("payments", True)
    secrets.put(paypal.SECRET_KEY, "pp-secret")
    PayPalSettings.objects.update_or_create(pk=1, defaults={"client_id": "pp-client"})

    def urlopen(request, timeout=None):
        body = b'{"error": "invalid_scope", "error_description": "No permission for scope"}'
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(body))

    monkeypatch.setattr(paypal.urllib.request, "urlopen", urlopen)
    with pytest.raises(paypal.PayPalError) as refused:
        paypal.check_credentials()
    assert refused.value.issue == "invalid_scope"
    assert "won't give the app Invoicing" in str(refused.value)


@pytest.mark.django_db
def test_paypal_settings_and_api(pp, taxes, customer, admin, admin_client):  # noqa: F811
    PayPalSettings.objects.filter(pk=1).update(enabled=False, client_id="")
    secrets.clear(paypal.SECRET_KEY)
    page = admin_client.get("/settings/online-payments/").content.decode()
    assert "Apps &amp; Credentials" in page
    response = admin_client.post("/settings/online-payments/paypal/", {"paypal-enabled": "on"})
    assert "client ID to take payments" in response.content.decode()
    response = admin_client.post(
        "/settings/online-payments/paypal/",
        {
            "paypal-enabled": "on",
            "paypal-sandbox": "on",
            "paypal-client_id": "pp-client",
            "paypal-client_secret": "pp-secret",
        },
    )
    assert response.status_code == 302 and secrets.get(paypal.SECRET_KEY) == "pp-secret"
    assert "pp-secret" not in admin_client.get("/settings/online-payments/").content.decode()
    admin_client.post("/settings/online-payments/paypal/test/")
    assert ("GET", "/v2/invoicing/invoices") in [c[:2] for c in pp.calls]

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

    shown = api("GET", "/settings")["paypal"]
    assert shown["active"] and shown["sandbox"] and shown["secret_set"]
    assert "pp-secret" not in json.dumps(shown)
    assert api("PATCH", "/settings", {"paypal": {"sandbox": False}})["paypal"]["sandbox"] is False
    api("PATCH", "/settings", {"paypal": {"sandbox": True}})
    invoice = _issued(customer, admin)
    api("POST", "/check")
    found = api("GET", f"/invoices/{invoice.pk}")
    assert found["links"][0]["provider"] == "paypal"
    assert found["links"][0]["number"] == invoice.number
    remade = api("POST", f"/invoices/{invoice.pk}/link", {"provider": "paypal"})
    assert remade["links"][0]["url"].endswith("INV2-TEST-0002")
    api("POST", f"/invoices/{invoice.pk}/link", {"provider": "stripe"}, expect=422)  # not on


def test_when_a_paypal_payment_was_made():
    """PayPal can mark its account's local clock time "Z": the reading nearer the
    invoice's last update (true UTC) is taken (seen 11/12/2026 in sandbox)."""
    import zoneinfo

    from trammel.payments.providers import paypal_paid_at

    pacific = zoneinfo.ZoneInfo("America/Los_Angeles")
    paid = {"payment_date": "2026-11-12", "payment_date_time": "2026-11-12T19:30:45Z"}
    found = {"detail": {"metadata": {"last_update_time": "2026-11-13T03:30:45Z"}}}
    with timezone.override(pacific):
        when = paypal_paid_at(paid, found)
        assert when == dt.datetime(2026, 11, 13, 3, 30, 45, tzinfo=dt.UTC)
        assert timezone.localdate(when) == dt.date(2026, 11, 12)
        # A true UTC time stays as it is.
        true = {"payment_date_time": "2026-11-13T03:30:45Z"}
        assert paypal_paid_at(true, found) == dt.datetime(2026, 11, 13, 3, 30, 45, tzinfo=dt.UTC)
        # A date alone: noon that day, locally.
        noon = paypal_paid_at({"payment_date": "2026-11-12"}, {})
        assert noon.astimezone(pacific) == dt.datetime(2026, 11, 12, 12, tzinfo=pacific)


@pytest.mark.django_db
def test_switching_to_live_retires_the_sandbox_page(pp, taxes, customer, admin):  # noqa: F811
    """A page made in sandbox is never shown once PayPal is live (seen 11/12/2026):
    retired here (live credentials can't reach a sandbox invoice to cancel it, and
    no real money moves through one), and a live one made in its place."""
    invoice = _issued(customer, admin)
    services.keep_links()
    sandbox = PayLink.objects.get()
    assert sandbox.test_mode and billing.pay_options(invoice)
    PayPalSettings.objects.filter(pk=1).update(sandbox=False)
    assert billing.pay_options(invoice) == []  # not even before the next check
    pp.calls.clear()
    assert services.keep_links() == 1
    assert not any(c[1].endswith("/cancel") for c in pp.calls)  # not reachable to cancel
    sandbox.refresh_from_db()
    assert not sandbox.active
    made_live = PayLink.objects.get(active=True)
    assert not made_live.test_mode and made_live.link_id != sandbox.link_id
    assert billing.pay_url(invoice) == made_live.url
