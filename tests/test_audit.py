"""FR-AUD: append-only audit log with before/after values."""

import datetime as dt

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from trammel.core import audit
from trammel.core.access import SYSTEM
from trammel.core.models import AuditEvent


@pytest.mark.django_db
def test_update_records_only_changed_fields(customer, admin):
    before = audit.snapshot(customer)
    customer.phone = "555-0199"
    customer.save()
    with audit.acting_as(admin, ip="192.0.2.7"):
        audit.record_update(customer, before)
    event = AuditEvent.objects.get(action="update")
    assert event.before == {"phone": "555-0100"} and event.after == {"phone": "555-0199"}
    assert (event.user, event.actor_kind, event.ip) == (admin, "user", "192.0.2.7")


@pytest.mark.django_db
def test_no_event_when_nothing_changed(customer):
    audit.record_update(customer, audit.snapshot(customer))
    assert not AuditEvent.objects.exists()


@pytest.mark.django_db
def test_outside_a_request_the_actor_is_the_system(setup_done):
    event = audit.record("test.thing", summary="hello")
    assert event.actor_kind == SYSTEM.kind and event.source == "system"


@pytest.mark.django_db
def test_audit_log_refuses_edits_and_deletes(setup_done):
    event = audit.record("test.thing")
    event.summary = "rewritten"
    with pytest.raises(ValidationError):
        event.save()
    with pytest.raises(ValidationError):
        event.delete()
    with pytest.raises(ValidationError):
        AuditEvent.objects.update(summary="x")
    with pytest.raises(ValidationError):
        AuditEvent.objects.all().delete()


@pytest.mark.django_db
def test_database_trigger_refuses_raw_updates(setup_done):
    audit.record("test.thing")
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE core_auditevent SET summary = 'tampered'")


@pytest.mark.django_db
def test_retention_purge_is_the_only_way_out(setup_done):
    AuditEvent.objects.create(
        action="test.old", actor_kind="system", at=timezone.now() - dt.timedelta(days=400)
    )
    audit.record("test.new")
    assert AuditEvent.purge_before(timezone.now() - dt.timedelta(days=365)) == 1
    assert list(AuditEvent.objects.values_list("action", flat=True)) == ["test.new"]


@pytest.mark.django_db
def test_secrets_never_reach_the_log(admin):
    assert "password" not in audit.snapshot(admin)
