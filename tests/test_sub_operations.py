"""Sub-operations: steps of an operation, up to four levels (FR-WO-2)."""

import json

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from trammel.accounts.models import ApiKey
from trammel.core import access
from trammel.customers.models import Customer
from trammel.jobs import services as jobs
from trammel.jobs.models import Job
from trammel.workorders import services
from trammel.workorders.models import Operation


@pytest.fixture
def wo(admin):
    job = jobs.create_job(Job(title="Cabinet", customer=Customer.internal()))
    jobs.transition(job, "approved", actor=admin)
    return services.create(job, title="Build the cabinet")


def _numbers(wo):
    return [(op.number, op.name) for op in services.operation_tree(wo)]


def _status(op):
    op.refresh_from_db()
    return op.status


@pytest.mark.django_db
def test_tree_is_numbered_in_reading_order(wo):
    case = services.add_operation(wo, name="Case")
    services.add_operation(wo, name="Doors")
    cut = services.add_operation(wo, name="Cut panels", parent=case)
    services.add_operation(wo, name="Rip", parent=cut)
    services.add_operation(wo, name="Glue up", parent=case)
    assert _numbers(wo) == [
        ("1", "Case"),
        ("1.1", "Cut panels"),
        ("1.1.1", "Rip"),
        ("1.2", "Glue up"),
        ("2", "Doors"),
    ]


@pytest.mark.django_db
def test_four_levels_and_no_more(wo):
    op = services.add_operation(wo, name="L1")
    for level in (2, 3, 4):
        op = services.add_operation(wo, name=f"L{level}", parent=op)
    assert op.depth == Operation.MAX_DEPTH
    with pytest.raises(ValidationError, match="at most 4 levels"):
        services.add_operation(wo, name="L5", parent=op)


@pytest.mark.django_db
def test_parent_follows_its_steps(wo, admin):
    case = services.add_operation(wo, name="Case")
    cut = services.add_operation(wo, name="Cut", parent=case)
    glue = services.add_operation(wo, name="Glue", parent=case)
    with pytest.raises(ValidationError, match="2 still to do"):
        services.set_operation(case, "done", user=admin)
    services.set_operation(cut, "done", user=admin)
    assert _status(case) == "pending"
    services.set_operation(glue, "skipped", user=admin)
    assert _status(case) == "done"
    # Reopening a step reopens the parent; so does adding a new step.
    services.set_operation(cut, "pending", user=admin)
    assert _status(case) == "pending"
    services.set_operation(cut, "done", user=admin)
    services.add_operation(wo, name="Sand", parent=case)
    assert _status(case) == "pending"


@pytest.mark.django_db
def test_skipping_a_parent_skips_what_is_left_and_all_skipped_reads_skipped(wo, admin):
    case = services.add_operation(wo, name="Case")
    cut = services.add_operation(wo, name="Cut", parent=case)
    glue = services.add_operation(wo, name="Glue", parent=case)
    services.set_operation(cut, "done", user=admin)
    services.set_operation(case, "skipped", user=admin)
    assert (_status(cut), _status(glue), _status(case)) == ("done", "skipped", "skipped")
    other = services.add_operation(wo, name="Hardware")
    a = services.add_operation(wo, name="Hinges", parent=other)
    services.set_operation(a, "skipped", user=admin)
    assert _status(other) == "skipped"


@pytest.mark.django_db
def test_work_order_completion_still_waits_for_every_step(wo, admin):
    from trammel.core.lifecycle import TransitionError

    case = services.add_operation(wo, name="Case")
    cut = services.add_operation(wo, name="Cut", parent=case)
    services.set_operation(services.add_operation(wo, name="Finish"), "done", user=admin)
    services.confirm_materials(wo)
    wo.refresh_from_db()
    with pytest.raises(TransitionError, match="not done or skipped"):
        services.transition(wo, "complete", actor=admin)
    services.set_operation(cut, "done", user=admin)
    assert _status(case) == "done"
    services.transition(wo, "complete", actor=admin)


@pytest.mark.django_db
def test_moving_swaps_only_siblings(wo):
    a = services.add_operation(wo, name="A")
    a1 = services.add_operation(wo, name="A1", parent=a)
    a2 = services.add_operation(wo, name="A2", parent=a)
    b = services.add_operation(wo, name="B")
    services.move_operation(a2, -1)
    services.move_operation(a1, +1)  # already last among A's steps: no change
    services.move_operation(b, -1)
    assert [n for _, n in _numbers(wo)] == ["B", "A", "A2", "A1"]
    assert {o.name: o.number for o in services.operation_tree(wo)}["A1"] == "2.2"
    assert a.pk and b.pk


@pytest.mark.django_db
def test_reparenting_checks_cycles_and_depth(wo, admin):
    a = services.add_operation(wo, name="A")
    a1 = services.add_operation(wo, name="A1", parent=a)
    a11 = services.add_operation(wo, name="A11", parent=a1)
    b = services.add_operation(wo, name="B")
    with pytest.raises(ValidationError, match="itself or its own"):
        services.reparent_operation(a, a11)
    b1 = services.add_operation(wo, name="B1", parent=b)
    b11 = services.add_operation(wo, name="B11", parent=b1)
    # A has two levels below it; under B11 (depth 2) it would reach depth 5.
    with pytest.raises(ValidationError, match="at most 4 levels"):
        services.reparent_operation(a, b11)
    services.reparent_operation(a, b)
    a11.refresh_from_db()
    assert a11.depth == 3 and [n for n, _ in _numbers(wo)][-1] == "1.2.1.1"
    options = services.parent_options(services.operation_tree(wo))
    # A has two levels below it, so it fits only at depth 1: under B, not B1 or B11.
    assert {o.name for o in options[a.pk]} == {"B"}
    services.reparent_operation(a, None, user=admin)
    a11.refresh_from_db()
    assert a11.depth == 2


@pytest.mark.django_db
def test_deleting_a_parent_deletes_its_steps_and_keeps_time(wo, admin):
    from trammel.timetracking.models import TimeEntry

    a = services.add_operation(wo, name="A")
    a1 = services.add_operation(wo, name="A1", parent=a)
    keep = services.add_operation(wo, name="Keep", parent=a)
    gone = services.add_operation(wo, name="Gone", parent=a1)
    TimeEntry.objects.create(
        work_order=wo, operation=gone, user=admin, kind="labor", started_at=timezone.now()
    )
    assert services.delete_operation(a1) == 1
    assert [n for _, n in _numbers(wo)] == ["A", "Keep"]
    assert TimeEntry.objects.get().operation is None
    services.set_operation(keep, "done", user=admin)
    assert _status(a) == "done"


@pytest.mark.django_db
def test_page_traveler_and_floor_show_the_tree(admin_client, wo):
    a = services.add_operation(wo, name="Case")
    services.add_operation(wo, name="Cut panels", parent=a)
    page = admin_client.get(f"/work-orders/{wo.pk}/?tab=operations").content.decode()
    assert 'class="op-number">1.1</span> Cut panels' in page and "op-depth-1" in page
    assert "Done when its steps are" in page
    response = admin_client.post(
        f"/work-orders/{wo.pk}/operations/new/", {"name": "Dovetails", "parent": a.pk}
    )
    assert response.status_code == 302
    assert ("1.2", "Dovetails") in _numbers(wo)
    traveler = admin_client.get(f"/work-orders/{wo.pk}/traveler/").content.decode()
    assert '<td class="num">1.2</td>' in traveler
    floor = admin_client.get(f"/floor/wo/{wo.pk}/").content.decode()
    assert 'class="op-number">1.1</span> Cut panels' in floor


@pytest.mark.django_db
def test_edit_form_moves_an_operation_under_another(admin_client, wo):
    a = services.add_operation(wo, name="A")
    b = services.add_operation(wo, name="B")
    response = admin_client.post(
        f"/work-orders/{wo.pk}/operations/{b.pk}/edit/", {"name": "B", "parent": a.pk}
    )
    assert response.status_code == 302
    b.refresh_from_db()
    assert b.parent == a and b.depth == 1


@pytest.mark.django_db
def test_api_adds_and_lists_sub_operations(admin, wo):
    token = ApiKey.issue(admin, "t", sorted(access.permissions_of(access.as_actor(admin))))
    token = token[1] if isinstance(token, tuple) else token
    client = Client(HTTP_AUTHORIZATION=f"Bearer {token}")

    def post(path, body):
        return client.post(
            f"/api/v1/work-orders/{wo.pk}{path}", json.dumps(body), content_type="application/json"
        )

    top = post("/operations", {"name": "Case"})
    assert top.status_code == 201, top.content
    parent = top.json()["operations"][0]["id"]
    child = post("/operations", {"name": "Cut", "parent": parent}).json()["operations"][1]
    assert (child["number"], child["parent"], child["depth"]) == ("1.1", parent, 1)
    assert post("/operations", {"name": "X", "parent": str(wo.pk)}).status_code == 422
    done = post(f"/operations/{parent}", {"state": "done"})
    assert done.status_code == 422 and "still to do" in done.content.decode()
