"""The business logo and the per-detail switches for documents (FR-ADM-3)."""

import base64
import hashlib
import importlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from trammel.core import templating
from trammel.core.forms import BusinessProfileForm
from trammel.core.models import AuditEvent, BusinessProfile

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
    b'<rect width="40" height="20"/></svg>'
)


def _png(size=(40, 20), mode="RGBA") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, (200, 40, 40, 255) if mode == "RGBA" else (200, 40, 40)).save(out, "PNG")
    return out.getvalue()


def _jpeg_with_exif(size=(3000, 1000)) -> bytes:
    image = Image.new("RGB", size, (10, 120, 200))
    exif = Image.Exif()
    exif[0x010F] = "SnoopCam"  # camera make
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif)
    return out.getvalue()


def _post(admin_client, business, **extra):
    form = BusinessProfileForm(instance=business, tab="documents")
    data = {}
    for name in form.fields:
        if name in ("logo_file", "remove_logo"):
            continue
        value = form[name].value()
        if isinstance(value, bool):
            if value:
                data[name] = "on"
        elif value is not None:
            data[name] = value
    data.update(extra)
    return admin_client.post("/settings/business/?tab=documents", data)


@pytest.mark.django_db
def test_upload_png_logo(admin_client, setup_done):
    upload = SimpleUploadedFile("logo.png", _png(), content_type="image/png")
    response = _post(admin_client, setup_done, logo_file=upload)
    assert response.status_code == 302
    business = BusinessProfile.load()
    assert business.logo_type == "image/png"
    assert bytes(business.logo).startswith(b"\x89PNG")
    assert business.logo_data_uri.startswith("data:image/png;base64,")
    # The audit log notes the change by size and fingerprint, not the image itself.
    event = AuditEvent.objects.filter(entity_type="core.businessprofile").latest("at")
    assert "bytes, sha256" in json.dumps(event.after)
    # The settings page shows the current logo and offers to remove it.
    page = admin_client.get("/settings/business/?tab=documents").content.decode()
    assert 'alt="Current logo"' in page and "Remove the logo" in page


@pytest.mark.django_db
def test_large_jpeg_is_scaled_and_stripped(admin_client, setup_done):
    upload = SimpleUploadedFile("photo.jpg", _jpeg_with_exif(), content_type="image/jpeg")
    _post(admin_client, setup_done, logo_file=upload)
    business = BusinessProfile.load()
    assert business.logo_type == "image/jpeg"
    with Image.open(io.BytesIO(bytes(business.logo))) as image:
        assert max(image.size) == 1600
        assert not image.getexif()


@pytest.mark.django_db
def test_svg_logo_kept_as_drawn(admin_client, setup_done):
    upload = SimpleUploadedFile("logo.svg", SVG, content_type="image/svg+xml")
    _post(admin_client, setup_done, logo_file=upload)
    business = BusinessProfile.load()
    assert business.logo_type == "image/svg+xml" and bytes(business.logo) == SVG


@pytest.mark.django_db
@pytest.mark.parametrize(
    "content,name",
    [
        (SVG.replace(b"<rect", b"<script>alert(1)</script><rect"), "evil.svg"),
        (SVG.replace(b"<rect", b'<rect onload="alert(1)"'), "evil.svg"),
        (b"%PDF-1.7 not a logo", "logo.png"),  # the name is not believed
        (b"\x89PNG\r\n\x1a\n" + b"garbage" * 20, "broken.png"),
    ],
)
def test_bad_logos_refused(admin_client, setup_done, content, name):
    response = _post(admin_client, setup_done, logo_file=SimpleUploadedFile(name, content))
    assert response.status_code == 200
    assert not BusinessProfile.load().logo


@pytest.mark.django_db
def test_remove_logo(admin_client, setup_done):
    setup_done.logo, setup_done.logo_type = _png(), "image/png"
    setup_done.save()
    _post(admin_client, setup_done, remove_logo="on")
    business = BusinessProfile.load()
    assert not business.logo and business.logo_type == ""


@pytest.mark.django_db
def test_hidden_details_reach_templates_empty(setup_done):
    business = setup_done
    business.address_line1, business.city, business.region = "1 Main St", "Olympia", "WA"
    business.phone, business.email = "555-0100", "shop@example.com"
    business.website = "https://shop.example.com"
    business.tax_registration_numbers = "Permit 123"
    business.logo, business.logo_type = _png(), "image/png"
    business.save()

    shown = templating.business_context(business)
    assert shown["address_lines"] and shown["phone"] and shown["logo"].startswith("data:")

    for flag in BusinessProfile.DOCUMENT_DETAILS:
        setattr(business, flag, False)
    hidden = templating.business_context(business)
    assert hidden["address_lines"] == [] and hidden["tax_registration_numbers"] == []
    assert hidden["phone"] == hidden["email"] == hidden["website"] == hidden["logo"] == ""
    assert hidden["name"] == "Test Shop"  # the name always shows


@pytest.mark.django_db
def test_default_templates_show_logo_and_website(setup_done):
    from trammel.billing.seed import DEFAULT_TEMPLATE as INVOICE
    from trammel.quotes.seed import DEFAULT_TEMPLATE as QUOTE

    business = setup_done
    business.logo, business.logo_type = _png(), "image/png"
    business.website = "https://shop.example.com"
    business.save()
    from trammel.core import placeholders

    for key, body in (("quote", QUOTE), ("invoice", INVOICE)):
        # The preview's sample context: everything a real document would have.
        ctx, _ = placeholders.get(key).preview()
        html = templating.render(body, ctx)
        assert '<img class="logo" src="data:image/png;base64,' in html
        assert "https://shop.example.com" in html


@pytest.mark.django_db
@pytest.mark.parametrize(
    "module,model",
    [
        ("trammel.quotes.migrations.0005_default_template_logo", "quotes.QuoteTemplate"),
        ("trammel.billing.migrations.0006_default_template_logo", "billing.InvoiceTemplate"),
    ],
)
def test_migration_upgrades_only_untouched_defaults(monkeypatch, setup_done, module, model):
    migration = importlib.import_module(module)
    Template = apps.get_model(model)
    Template.all_objects.all().delete()
    shipped = Template.objects.create(name="Standard", body="the old default", is_default=True)
    edited = Template.objects.create(name="Mine", body="the shop's own")
    monkeypatch.setattr(migration, "SHIPPED_0_1_0", hashlib.sha256(b"the old default").hexdigest())
    migration.upgrade(apps, None)
    shipped.refresh_from_db()
    edited.refresh_from_db()
    assert "business.logo" in shipped.body
    assert edited.body == "the shop's own"


@pytest.mark.django_db
def test_export_writes_logo_as_base64(setup_done):
    from trammel.system import backup, export

    setup_done.logo, setup_done.logo_type = _png(), "image/png"
    setup_done.save()
    record = export.build()
    assert record.status == "ok"
    with zipfile.ZipFile(Path(backup.backup_dir()) / "exports" / record.filename) as zf:
        row = json.loads(zf.read("data/core.businessprofile.ndjson").splitlines()[0])
    assert base64.b64decode(row["logo"]).startswith(b"\x89PNG")


@pytest.mark.django_db
def test_agreement_print_shows_logo_unless_hidden(setup_done):
    from django.template.loader import render_to_string

    business = setup_done
    business.logo, business.logo_type = _png(), "image/png"
    agreement = {"job": {"number": "J-1", "title": "T", "customer": {"name": "C"}}, "text": "x"}
    html = render_to_string("agreements/print.html", {"agreement": agreement, "business": business})
    assert 'class="logo" src="data:image/png;base64,' in html
    business.show_logo = False
    html = render_to_string("agreements/print.html", {"agreement": agreement, "business": business})
    assert 'class="logo"' not in html
