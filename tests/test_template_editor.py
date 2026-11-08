"""Template editor: live preview of unsaved text, and the placeholder reference."""

import pytest

from trammel.core import placeholders
from trammel.quotes.models import QuoteTemplate


@pytest.mark.django_db
def test_every_template_kind_previews_with_sample_data(setup_done):
    keys = {k.key for k in placeholders.kinds()}
    assert {"quote", "invoice", "agreement", "email:quote_sent", "email:magic_link"} <= keys
    for kind in placeholders.kinds():
        ctx, source = kind.preview()
        assert source == "sample data"
        reference = placeholders.reference(kind, ctx)
        assert all(item["example"] for group in reference for item in group["items"])


@pytest.mark.django_db
def test_editor_page_has_preview_and_placeholders(admin_client):
    template = QuoteTemplate.objects.first()
    page = admin_client.get(f"/quotes/templates/{template.pk}/").content.decode()
    assert 'formtarget="template-preview"' in page
    assert "{{ quote.valid_until }}" in page and "Valid until" in page
    assert "Jinja" not in page


@pytest.mark.django_db
def test_preview_renders_what_was_typed_not_what_was_saved(admin_client):
    response = admin_client.post(
        "/settings/placeholders/quote/preview/",
        {"body": "<p>{{ quote.number }} for {{ customer.name }}: {{ quote.total|money }}</p>"},
    )
    assert response.status_code == 200
    assert "<p>Q-2026-0012 for Grace Hollis: $3,058.66</p>" in response.content.decode()
    assert "sandbox" in response["Content-Security-Policy"]
    assert response["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.django_db
def test_email_preview_shows_subject_and_problems(admin_client):
    response = admin_client.post(
        "/settings/placeholders/email:invoice_overdue/preview/",
        {"subject": "Overdue: {{ invoice.number }}", "body": "Balance {{ invoice.balance|money }}"},
    )
    page = response.content.decode()
    assert "Overdue: INV-2026-0031" in page and "Balance $1,529.33" in page
    broken = admin_client.post(
        "/settings/placeholders/agreement/preview/", {"body": "{% if %}"}
    ).content.decode()
    assert "This template has a problem" in broken


@pytest.mark.django_db
def test_preview_needs_the_template_permission(staff_client):
    assert (
        staff_client.post("/settings/placeholders/quote/preview/", {"body": "x"}).status_code == 403
    )
    assert staff_client.get("/settings/placeholders/").status_code == 403


@pytest.mark.django_db
def test_reference_page_lists_every_kind(admin_client):
    page = admin_client.get("/settings/placeholders/").content.decode()
    for title in ("Quotes", "Invoices", "Customer agreements", "Email: Overdue invoice reminder"):
        assert title in page
    assert "{{ invoice.balance }}" in page and "Jinja" not in page
