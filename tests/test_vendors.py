"""Vendors, and materials linked to them with a product page and files (FR-CUS-5)."""

import json
from decimal import Decimal

import pytest

from trammel.shop.models import Material, Vendor


@pytest.fixture
def vendor(setup_done):
    return Vendor.objects.create(name="Edensaw", payment_terms="Net 30", lead_time_days=2)


@pytest.mark.django_db
def test_vendor_screens(admin_client, vendor):
    Material.objects.create(
        name="Baltic birch",
        unit="sheet",
        unit_cost=Decimal("18.5"),
        vendor=vendor,
        url="https://example.com/birch",
    )
    page = admin_client.get("/shop/vendors/").content.decode()
    assert "Edensaw" in page and "Net 30" in page and "2 days" in page
    page = admin_client.get(f"/shop/vendors/{vendor.pk}/").content.decode()
    assert "Baltic birch" in page and "Price lists, quotes and files" in page
    response = admin_client.post(
        "/shop/vendors/new/", {"name": "Crosscut Hardwoods", "is_active": "on"}
    )
    assert response.status_code == 302 and Vendor.objects.filter(name="Crosscut Hardwoods").exists()


@pytest.mark.django_db
def test_material_links_a_vendor_and_a_product_page(admin_client, vendor):
    page = admin_client.get(f"/shop/materials/new/?vendor={vendor.pk}").content.decode()
    assert f'<option value="{vendor.pk}" selected>' in page
    response = admin_client.post(
        "/shop/materials/new/",
        {
            "name": "Walnut",
            "category": "lumber",
            "unit": "board_ft",
            "unit_cost": "14.25",
            "vendor": str(vendor.pk),
            "url": "https://example.com/walnut",
            "is_active": "on",
        },
    )
    assert response.status_code == 302
    walnut = Material.objects.get(name="Walnut")
    assert walnut.vendor == vendor and walnut.url == "https://example.com/walnut"
    page = admin_client.get(f"/shop/materials/{walnut.pk}/").content.decode()
    assert "Open the product page" in page and "Spec sheets, photos and files" in page
    listing = admin_client.get("/shop/materials/?q=edensaw").content.decode()
    assert "Walnut" in listing and "Product page" in listing


@pytest.mark.django_db
def test_inactive_vendors_are_not_offered_for_new_materials(admin_client, vendor):
    vendor.is_active = False
    vendor.save()
    page = admin_client.get("/shop/materials/new/").content.decode()
    assert "Edensaw" not in page


@pytest.mark.django_db
def test_vendor_api(admin, client):
    from trammel.accounts.models import ApiKey

    _key, token = ApiKey.issue(admin, "t", ["shop.view", "shop.edit"])
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    response = client.post(
        "/api/v1/shop/vendors",
        json.dumps({"name": "Printed Solid"}),
        content_type="application/json",
        **headers,
    )
    assert response.status_code == 201
    vid = response.json()["id"]
    response = client.post(
        "/api/v1/shop/materials",
        json.dumps({"name": "PLA", "unit": "kg", "unit_cost": "22", "vendor_id": vid}),
        content_type="application/json",
        **headers,
    )
    assert response.status_code == 201 and response.json()["vendor"] == vid
