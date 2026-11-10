"""Tax exemption certificates on file, and staff notices about stock and deliveries."""

import datetime as dt
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from tests.conftest import make_user
from tests.test_purchasing import _order, ply, shelf, vendor  # noqa: F401
from trammel.billing import services as billing
from trammel.core.lifecycle import TransitionError
from trammel.core.models import BusinessProfile
from trammel.files import services as files
from trammel.files.models import ShopDocument
from trammel.notifications.models import EmailLog, SmtpSettings
from trammel.purchasing import services as purchasing
from trammel.stock import services as stock
from trammel.stock.models import StockItem

D = Decimal
PDF = b"%PDF-1.4 certificate"


@pytest.fixture
def smtp(setup_done):
    s = SmtpSettings.load()
    s.host, s.from_address = "smtp.example.com", "shop@example.com"
    s.save()
    return s


def _resale(number="WA-RS-1188", expires=None):
    BusinessProfile.objects.filter(pk=1).update(
        resale_certificate_number=number, resale_certificate_expires=expires
    )


# ---------------------------------------------------------------------------
# A customer's exemption certificate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_exempt_customer_certificate_is_kept_with_their_files(admin_client, customer, admin):
    customer.tax_exempt, customer.tax_exempt_number = True, "RS-123"
    customer.save()
    invoice = billing.create(customer, kind="direct_sale", invoice_date=timezone.localdate())
    page = admin_client.get(customer.get_absolute_url()).content.decode()
    assert "No certificate on file" in page
    assert (
        "no exemption certificate on file"
        in admin_client.get(invoice.get_absolute_url()).content.decode()
    )

    admin_client.post(
        f"/files/attach/customers.customer/{customer.pk}/",
        {
            "file": SimpleUploadedFile("grace-resale.pdf", PDF),
            "doc_type": files.exemption_type().pk,
            "next": customer.get_absolute_url(),
        },
    )
    link = files.certificate_for(customer)
    assert link.document.title == "grace-resale.pdf"
    page = admin_client.get(customer.get_absolute_url()).content.decode()
    assert "No certificate on file" not in page and "grace-resale.pdf" in page
    assert (
        "no exemption certificate on file"
        not in admin_client.get(invoice.get_absolute_url()).content.decode()
    )


@pytest.mark.django_db
def test_a_file_already_uploaded_can_be_marked_the_certificate(admin_client, customer, admin):
    customer.tax_exempt, customer.tax_exempt_number = True, "RS-123"
    customer.save()
    files.upload(
        SimpleUploadedFile("scan-0042.pdf", PDF),
        name="scan-0042.pdf",
        attach_to=customer,
        user=admin,
    )
    assert files.certificate_for(customer) is None
    link = files.attachments_for(customer).get()
    page = admin_client.get(customer.get_absolute_url()).content.decode()
    assert "Or use a file already uploaded" in page
    admin_client.post("/files/certificate/", {"link": link.pk})
    assert files.certificate_for(customer) == link
    # Someone who can't see the customer can't mark their files.
    client = admin_client
    client.force_login(make_user("nosy@example.com", "files.upload", "files.view"))
    assert client.post("/files/certificate/", {"link": link.pk}).status_code == 404


@pytest.mark.django_db
def test_taxable_customers_have_no_certificate_panel(admin_client, customer):
    assert (
        "Tax exemption certificate"
        not in admin_client.get(customer.get_absolute_url()).content.decode()
    )


# ---------------------------------------------------------------------------
# The shop's resale certificate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_resale_certificate_number_and_file_in_business_settings(admin_client, client, admin):
    page = admin_client.get("/settings/business/?tab=tax").content.decode()
    assert "Resale certificate" in page and 'name="resale_certificate_number"' in page
    for name in ("resale-2026.pdf", "resale-2027.pdf"):
        admin_client.post(
            "/files/shop/resale_certificate/",
            {
                "file": SimpleUploadedFile(name, PDF + name.encode()),
                "next": "/settings/business/?tab=tax",
            },
        )
    document = files.shop_document(ShopDocument.RESALE_CERTIFICATE)
    assert document.revision_count == 2  # a new copy keeps the old one on record
    assert document.doc_type == files.exemption_type()
    assert "copy 2" in admin_client.get("/settings/business/?tab=tax").content.decode()
    client.force_login(make_user("floor@example.com", "files.upload", "files.view"))
    response = client.post(
        "/files/shop/resale_certificate/", {"file": SimpleUploadedFile("x.pdf", PDF)}
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_exempt_orders_use_the_shops_certificate(admin_client, vendor, ply, admin):  # noqa: F811
    response = admin_client.post(
        "/purchasing/new/",
        {"vendor": vendor.pk, "tax_exempt": "on", "tax": "0", "shipping": "0", "discount": "0"},
    )
    assert "resale certificate isn&#x27;t set" in response.content.decode()

    po = _order(vendor, (ply, "2", "40"), tax_exempt=True)
    assert po.exemption_number == ""
    with pytest.raises(TransitionError, match="resale certificate isn't set"):
        purchasing.place(po, actor=admin)
    _resale(expires=timezone.localdate() - dt.timedelta(days=1))
    with pytest.raises(TransitionError, match="expired"):
        purchasing.place(po, actor=admin)

    _resale()
    purchasing.place(po, actor=admin)
    po.refresh_from_db()
    assert po.exemption_number == "WA-RS-1188"
    _resale("WA-RS-2000")  # renewed later: the order keeps what it went out with
    purchasing.update_order(po, notes="Call before delivering")
    po.refresh_from_db()
    assert po.exemption_number == "WA-RS-1188"
    assert "WA-RS-1188" in admin_client.get(f"/purchasing/{po.pk}/document/").content.decode()


# ---------------------------------------------------------------------------
# Notices to staff
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_low_stock_is_mentioned_once_until_it_recovers(smtp, ply, admin):  # noqa: F811
    from trammel.stock.tasks import low_stock_notice

    low_stock_notice({})  # none on hand, restock at 5
    sent = EmailLog.objects.filter(event="stock_low")
    assert [e.subject for e in sent.all()] == ["Low stock: 1 material"]
    assert StockItem.objects.get(material=ply).low_notified_at is not None
    low_stock_notice({})
    assert sent.count() == 1
    stock.receive(ply, quantity=D("20"), total_cost=D("800"))
    low_stock_notice({})
    assert StockItem.objects.get(material=ply).low_notified_at is None
    stock.scrap(ply.stock.lots.get(), quantity=D("16"), note="Water damage")
    low_stock_notice({})
    assert sent.count() == 2


@pytest.mark.django_db
def test_late_deliveries_are_mentioned_once_per_expected_date(smtp, vendor, ply, admin):  # noqa: F811
    from trammel.purchasing.tasks import late_delivery_notice

    today = timezone.localdate()
    po = _order(vendor, (ply, "10", "40"), expected_on=today - dt.timedelta(days=2))
    late_delivery_notice({})
    sent = EmailLog.objects.filter(event="delivery_overdue")
    assert not sent.exists()  # a draft isn't on its way
    purchasing.place(po, actor=admin)
    late_delivery_notice({})
    late_delivery_notice({})
    assert sent.count() == 1 and po.number in sent.get().subject
    purchasing.receive(po.lines.get(), quantity=D("10"), user=admin)
    EmailLog.objects.all().delete()
    late_delivery_notice({})
    assert not sent.exists()  # it all came
