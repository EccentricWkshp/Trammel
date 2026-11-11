"""Form layout, number display, units, wording, hidden modules and email settings."""

import re
from decimal import Decimal

import pytest

from tests.conftest import make_user
from trammel.core import modules, panels, units
from trammel.core.money import trim_decimal
from trammel.core.navigation import menu_for
from trammel.core.panels import Panel
from trammel.notifications import services as notifications
from trammel.notifications.models import EmailTemplate, SmtpSettings

D = Decimal


def test_trim_decimal():
    assert trim_decimal(D("10.0000"), 2) == "10.00"
    assert trim_decimal(D("0.1500"), 2) == "0.15"
    assert trim_decimal(D("0.1234"), 2) == "0.1234"
    assert trim_decimal(D("30.000")) == "30"
    assert trim_decimal(D("12.500")) == "12.5"
    assert trim_decimal(D("100")) == "100"
    assert trim_decimal(D("0")) == "0"


@pytest.mark.django_db
def test_money_inputs_show_two_places_and_percentages_none(admin_client):
    from trammel.core.models import ShopRates

    rates = ShopRates.load()
    rates.shop_overhead_rate, rates.electricity_rate = D("10"), D("0.15")
    rates.default_material_markup = D("30")
    rates.save()
    page = admin_client.get("/settings/rates/").content.decode()
    assert 'value="10.00"' in page  # overhead rate, stored as 10.0000
    assert 'value="0.15"' in page  # electricity, stored as 0.1500
    assert "10.0000" not in page and "30.000" not in page


@pytest.mark.django_db
def test_every_field_has_a_wrapper_and_checkboxes_sit_beside_their_label(admin_client):
    page = admin_client.get("/accounts/users/new/").content.decode()
    assert '<div class="field field-check"><label class="check" for="id_is_admin">' in page
    assert page.count('<div class="field') >= 4


def test_us_volumes():
    assert units.convert(1, "gal", "qt") == 4
    assert units.convert(1, "qt", "pt") == 2
    assert units.convert(1, "pt", "fl_oz") == 16
    assert units.convert(1, "gal", "cu_in") == 231
    assert units.convert(1, "cu_yd", "cu_ft") == 27
    assert ("gal", "gallon") in dict(units.UNIT_CHOICES)["Volume"]


@pytest.mark.django_db
def test_tax_settings_name_no_particular_state(admin_client):
    page = admin_client.get("/tax/settings/").content.decode()
    # As a word: the page's random CSRF token can contain the letters.
    assert not re.search(r"WA", page)
    assert "Washington" not in page and "B&amp;O" not in page


@pytest.mark.django_db
def test_a_disabled_module_leaves_the_menus(admin):
    assert "Reports" in [i.label for i in menu_for(admin, "main")]
    modules.set_enabled("reports", False)
    assert "Reports" not in [i.label for i in menu_for(admin, "main")]


@pytest.mark.django_db
def test_a_disabled_module_leaves_record_pages(rf, admin):
    def provider(request, obj):
        return Panel("Widgets", "x.html")

    provider.__module__ = "tests.testapp.models"
    panels.register("accounts.user")(provider)
    try:
        request = rf.get("/")
        assert [p.title for p in panels.for_object(request, admin)] == ["Widgets"]
        modules.set_enabled("testapp", False)
        assert panels.for_object(request, admin) == []
    finally:
        panels._providers["accounts.user"].remove(provider)


@pytest.mark.django_db
def test_staff_recipients_are_chosen_users(admin_client, admin):
    riley = make_user("riley@example.com", name="Riley Chen")
    make_user("nomail@example.com", name="No Mail")
    page = admin_client.get("/settings/email/notifications/").content.decode()
    assert 'class="multiselect"' in page and "Riley Chen &lt;riley@example.com&gt;" in page
    assert notifications.staff_recipients() == [admin.email]  # nobody chosen: system admins
    response = admin_client.post(
        "/settings/email/notifications/",
        {
            "overdue_reminder_days": "7",
            "staff_recipients": [str(riley.pk)],
        },
    )
    assert response.status_code == 302
    assert list(SmtpSettings.load().staff_recipients.all()) == [riley]
    assert notifications.staff_recipients() == ["riley@example.com"]


@pytest.mark.django_db
def test_notifications_switch_on_and_off_from_the_list(admin_client):
    admin_client.post("/settings/email/templates/quote_sent/toggle/", {"enabled": "0"})
    assert not EmailTemplate.objects.get(event="quote_sent").enabled
    page = admin_client.get("/settings/email/notifications/").content.decode()
    assert 'role="switch" aria-checked="false"' in page
    admin_client.post("/settings/email/templates/quote_sent/toggle/", {"enabled": "1"})
    assert EmailTemplate.objects.get(event="quote_sent").enabled


@pytest.mark.django_db
def test_sign_in_link_goes_to_the_person(admin_client):
    page = admin_client.get("/settings/email/notifications/").content.decode()
    assert "Person signing in" in page
