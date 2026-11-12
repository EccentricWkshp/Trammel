"""SPEC §4.5 / FR-ADM-7: module registry, dependencies, read-only retention."""

import pytest
from django.core.exceptions import ValidationError

from tests.testapp.models import Widget
from trammel.core import modules
from trammel.core.models import AuditEvent, DocumentSequence
from trammel.core.navigation import menu_for


@pytest.mark.django_db
def test_installed_modules_register_and_sync(setup_done):
    keys = [m.key for m in modules.manifests()]
    assert keys[:3] == ["core", "accounts", "customers"]  # core first
    assert "testapp" in keys
    # Number series declared in a manifest exist after migrate.
    assert DocumentSequence.objects.filter(key__in=["widget", "ticket"]).count() == 2
    assert modules.check_dependencies() == []


@pytest.mark.django_db
def test_core_modules_cannot_be_disabled(setup_done):
    with pytest.raises(ValidationError, match="core module"):
        modules.set_enabled("customers", False)


@pytest.mark.django_db
def test_disable_and_enable_are_audited(setup_done):
    modules.set_enabled("testapp", False, reason="not a widget shop")
    assert not modules.is_enabled("testapp")
    modules.set_enabled("testapp", True)
    actions = list(AuditEvent.objects.order_by("at").values_list("action", "reason"))
    assert actions == [("module.disable", "not a widget shop"), ("module.enable", "")]


@pytest.mark.django_db
def test_admin_screen_toggles_module(admin_client):
    response = admin_client.post("/settings/modules/testapp/", {"enable": "0"})
    assert response.status_code == 302 and not modules.is_enabled("testapp")
    assert admin_client.post("/settings/modules/customers/", {"enable": "0"}).status_code == 302
    assert modules.is_enabled("customers")


@pytest.mark.django_db
def test_module_screen_is_admin_only(staff_client):
    assert staff_client.get("/settings/modules/").status_code == 403
    assert staff_client.post("/settings/modules/testapp/", {"enable": "0"}).status_code == 403


@pytest.mark.django_db
def test_currency_locks_once_a_financial_document_exists(admin_client):
    assert not modules.currency_locked()
    Widget.objects.create(name="first invoice stand-in")
    assert modules.currency_locked()
    page = admin_client.get("/settings/business/?tab=region").content.decode()
    assert "Locked" in page


@pytest.mark.django_db
def test_menus_follow_permissions(admin, staff):
    assert "Customers" in [i.label for i in menu_for(staff, "main")]
    assert menu_for(staff, "admin") == []
    admin_labels = [i.label for i in menu_for(admin, "admin")]
    assert {"Business", "Users", "Roles", "Modules", "Audit log"} <= set(admin_labels)


@pytest.mark.django_db
def test_the_modules_page_lists_core_first_then_the_rest_by_name(admin_client):
    from trammel.core import modules as registry

    rows = admin_client.get("/settings/modules/").context["rows"]
    names = [r["m"].name for r in rows]
    core = [m.name for m in registry.manifests() if m.core]
    assert names[: len(core)] == core
    rest = names[len(core) :]
    assert rest == sorted(rest, key=str.casefold) and rest
