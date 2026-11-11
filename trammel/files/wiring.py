"""Certificates shown on other modules' pages: the customer's tax exemption
certificate, the shop's resale certificate in business settings, and a warning on
an exempt invoice whose customer has none on file."""

from django.utils import timezone

from trammel.core import access, panels
from trammel.core.panels import Panel

from . import services
from .models import ShopDocument


@panels.tab_count("files")
def file_count(request, obj):
    """The Files tab counts the files it lists, for those who can see them."""
    if not access.has_perm(request.actor, "files.view"):
        return None
    return services.attachments_for(obj).count()


@panels.register("customers.customer")
def exemption_certificate(request, customer):
    link = services.certificate_for(customer)
    if not (customer.tax_exempt or link) or not access.has_perm(request.actor, "files.view"):
        return None
    can_upload = access.has_perm(request.actor, "files.upload")
    others = []
    if can_upload:
        others = [
            a
            for a in services.attachments_for(customer)
            if a.document.doc_type is None or a.document.doc_type.slug != services.EXEMPTION
        ]
    return Panel(
        "Tax exemption certificate",
        "files/_certificate_panel.html",
        {
            "customer": customer,
            "link": link,
            "doc_type": services.exemption_type(),
            "others": others,
            "can_upload": can_upload,
            "here": request.get_full_path(),
        },
        order=5,
    )


@panels.register("core.businessprofile")
def resale_certificate(request, business):
    if not access.has_perm(request.actor, "files.view"):
        return None
    return Panel(
        "Resale certificate file",
        "files/_resale_certificate_panel.html",
        {
            "document": services.shop_document(ShopDocument.RESALE_CERTIFICATE),
            "key": ShopDocument.RESALE_CERTIFICATE,
            "can_upload": access.has_perm(request.actor, "files.upload"),
            "here": request.get_full_path(),
        },
        tab="tax",  # beside the certificate's number
    )


@panels.register("billing.invoice")
def invoice_certificate(request, invoice):
    """An exempt invoice should have the customer's certificate behind it."""
    exempt = invoice.exemption_number or (
        invoice.is_draft and invoice.customer.exemption_valid_on(timezone.localdate())
    )
    if not exempt or services.certificate_for(invoice.customer):
        return None
    return Panel(
        "Tax exemption certificate",
        "files/_certificate_missing.html",
        {"customer": invoice.customer, "invoice": invoice},
        order=1,
    )
