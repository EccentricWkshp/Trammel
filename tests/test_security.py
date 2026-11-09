"""SPEC §9 headers and the pages every signed-in user can reach."""

import pytest


@pytest.mark.django_db
def test_security_headers(admin_client):
    response = admin_client.get("/")
    csp = response["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "object-src 'none'" in csp
    assert "unsafe-inline" not in csp
    assert response["X-Frame-Options"] == "DENY"
    assert response["X-Content-Type-Options"] == "nosniff"


@pytest.mark.django_db
def test_anonymous_users_are_sent_to_sign_in(client, setup_done):
    for path in ("/", "/customers/", "/settings/business/", "/accounts/users/"):
        response = client.get(path)
        assert response.status_code == 302 and response.url.startswith("/accounts/login/")
    assert client.get("/api/v1/customers/").status_code == 401


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/customers/",
        "/customers/new/",
        "/accounts/profile/",
        "/accounts/users/",
        "/accounts/users/new/",
        "/accounts/roles/",
        "/accounts/roles/new/",
        "/settings/business/",
        "/settings/rates/",
        "/settings/system/",
        "/settings/numbers/",
        "/settings/numbers/widget/",
        "/settings/modules/",
        "/settings/audit/",
        "/settings/audit.csv",
        "/accounts/profile/api-keys/new/",
        "/api/v1/docs",
    ],
)
def test_admin_pages_render(admin_client, path):
    assert admin_client.get(path).status_code == 200


@pytest.mark.django_db
def test_api_docs_load_nothing_from_outside(admin_client):
    # The content security policy allows scripts and styles from this site only;
    # docs that pull Swagger UI from a CDN render as a blank page.
    page = admin_client.get("/api/v1/docs").content.decode()
    assert "/static/ninja/swagger-ui-bundle.js" in page
    assert "/static/ninja/swagger-ui.css" in page
    assert "cdn." not in page and "<script>" not in page


@pytest.mark.django_db
def test_settings_save_is_audited(admin_client):
    from trammel.core.models import AuditEvent, ShopRates

    response = admin_client.post(
        "/settings/rates/",
        {
            "labor_hourly_rate": "85.00",
            "labor_daily_rate": "600",
            "shop_overhead_rate": "12.5",
            "default_material_markup": "25",
            "consumables_allowance": "5",
            "minimum_job_charge": "50",
            "electricity_rate": "0.17",
        },
    )
    assert response.status_code == 302
    assert str(ShopRates.load().labor_hourly_rate) == "85.00"
    event = AuditEvent.objects.get(action="update")
    assert event.after["labor_hourly_rate"] == "85.00"


@pytest.mark.django_db
def test_dates_display_in_us_format_by_default(admin_client, customer):
    import datetime as dt

    from trammel.core.models import BusinessProfile
    from trammel.jobs import services
    from trammel.jobs.models import Job

    services.create_job(Job(title="Sign", customer=customer, due_date=dt.date(2026, 3, 4)))
    assert "03/04/2026" in admin_client.get("/jobs/").content.decode()
    business = BusinessProfile.load()
    business.date_format = "iso"
    business.save()
    assert "2026-03-04" in admin_client.get("/jobs/").content.decode()
