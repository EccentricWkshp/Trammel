"""SPEC §3.3 and §6: no skipped states, reopen one step with a reason, forced close."""

import pytest
from django.core.exceptions import PermissionDenied

from tests.conftest import make_user
from tests.testapp.models import Widget
from trammel.core.lifecycle import TransitionError
from trammel.core.models import AuditEvent, StateChange

life = Widget.lifecycle


@pytest.fixture
def widget(setup_done):
    return Widget.objects.create(name="Bracket")


@pytest.mark.django_db
def test_forward_path_records_history_and_audit(widget, admin):
    life.apply(widget, "active", actor=admin)
    life.apply(widget, "done", actor=admin)
    widget.refresh_from_db()
    assert widget.status == "done"
    assert [(c.from_state, c.to_state) for c in widget.history()] == [
        ("open", "active"),
        ("active", "done"),
    ]
    assert AuditEvent.objects.filter(action="transition", entity_id=str(widget.pk)).count() == 2


@pytest.mark.django_db
def test_states_cannot_be_skipped(widget, admin):
    with pytest.raises(TransitionError):
        life.apply(widget, "done", actor=admin)
    with pytest.raises(TransitionError):
        life.apply(widget, "closed", actor=admin)  # only as a forced close


@pytest.mark.django_db
def test_status_cannot_be_assigned_directly(widget):
    widget.status = "closed"
    with pytest.raises(TransitionError):
        widget.save()
    with pytest.raises(TransitionError):
        Widget.objects.create(name="x", status="done")


@pytest.mark.django_db
def test_editing_a_stale_instance_does_not_undo_a_transition(widget, admin):
    stale = Widget.objects.get(pk=widget.pk)
    life.apply(widget, "active", actor=admin)
    stale.name = "Renamed"
    stale.save()
    fresh = Widget.objects.get(pk=widget.pk)
    assert (fresh.name, fresh.status) == ("Renamed", "active")


@pytest.mark.django_db
def test_guards_block_and_list_every_problem(widget, admin):
    Widget.objects.filter(pk=widget.pk).update(open_parts=2)
    widget.refresh_from_db()
    life.apply(widget, "active", actor=admin)
    with pytest.raises(TransitionError) as exc:
        life.apply(widget, "done", actor=admin)
    assert exc.value.problems == ["2 part(s) still open."]


@pytest.mark.django_db
def test_reopen_needs_permission_and_reason(widget, admin, setup_done):
    for target in ("active", "done", "closed"):
        life.apply(widget, target, actor=admin)
    clerk = make_user("clerk@example.com", "testapp.view")
    with pytest.raises(PermissionDenied):
        life.apply(widget, "done", actor=clerk, reason="missed a hole")
    with pytest.raises(TransitionError, match="reason"):
        life.apply(widget, "done", actor=admin, reason="  ")
    change = life.apply(widget, "done", actor=admin, reason="Missed a hole")
    assert change.kind == StateChange.Kind.REOPEN and change.reason == "Missed a hole"
    assert AuditEvent.objects.filter(action="reopen", reason="Missed a hole").exists()


@pytest.mark.django_db
def test_forced_close_skips_guards_and_stores_outstanding_state(widget, admin):
    Widget.objects.filter(pk=widget.pk).update(open_parts=3)
    widget.refresh_from_db()
    change = life.apply(
        widget,
        "closed",
        actor=admin,
        reason="Customer vanished",
        kind="force",
        details={"open_parts": 3},
    )
    assert change.kind == "force" and change.details == {"open_parts": 3}
    widget.refresh_from_db()
    assert widget.status == "closed"


@pytest.mark.django_db
def test_available_transitions_respect_permissions(widget, admin, setup_done):
    clerk = make_user("clerk2@example.com", "testapp.view")
    assert {t.target for t in life.available(widget, clerk)} == {"active"}
    assert {t.target for t in life.available(widget, admin)} == {"active", "closed"}
