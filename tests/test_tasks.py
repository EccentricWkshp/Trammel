"""SPEC §3.5: background tasks on PostgreSQL."""

from datetime import timedelta

import pytest
from django.utils import timezone

from trammel.core import secrets, tasks
from trammel.core.models import BackgroundTask

calls = []


@tasks.task("test.record")
def _record(payload):
    calls.append(payload)


@tasks.task("test.fail")
def _fail(payload):
    raise RuntimeError("nope")


@tasks.periodic("test.tick", every=timedelta(hours=1))
def _tick(payload):
    calls.append("tick")


@pytest.fixture(autouse=True)
def _reset():
    calls.clear()


@pytest.mark.django_db
def test_enqueue_and_run():
    tasks.enqueue("test.record", {"n": 1})
    assert tasks.run_pending() == 1
    assert calls == [{"n": 1}]
    assert BackgroundTask.objects.get().state == "done"


@pytest.mark.django_db
def test_future_tasks_wait():
    tasks.enqueue("test.record", {"n": 2}, run_after=timezone.now() + timedelta(minutes=5))
    assert tasks.run_pending() == 0


@pytest.mark.django_db
def test_failures_retry_with_backoff_then_fail():
    row = tasks.enqueue("test.fail")
    tasks.run_pending()
    row.refresh_from_db()
    assert row.state == "pending" and row.attempts == 1 and "nope" in row.last_error
    assert row.run_after > timezone.now()
    BackgroundTask.objects.filter(pk=row.pk).update(
        attempts=row.max_attempts - 1, run_after=timezone.now()
    )
    tasks.run_pending()
    row.refresh_from_db()
    assert row.state == "failed"


@pytest.mark.django_db
def test_dedupe_and_unknown_names():
    tasks.enqueue("test.record", {"x": 1}, dedupe=True)
    assert tasks.enqueue("test.record", {"x": 1}, dedupe=True) is None
    with pytest.raises(LookupError):
        tasks.enqueue("test.nothing")


@pytest.mark.django_db
def test_periodic_tasks_are_scheduled_once_per_interval():
    assert tasks.schedule_periodic() >= 1
    tasks.run_pending()
    assert "tick" in calls
    calls.clear()
    tasks.schedule_periodic()
    tasks.run_pending()
    assert "tick" not in calls


@pytest.mark.django_db
def test_stale_running_tasks_are_reclaimed():
    row = tasks.enqueue("test.record")
    BackgroundTask.objects.filter(pk=row.pk).update(
        state="running", locked_at=timezone.now() - timedelta(hours=2)
    )
    assert tasks.reclaim_stale(60) == 1


@pytest.mark.django_db
def test_secrets_are_encrypted_at_rest():
    from trammel.core.models import Credential

    secrets.put("smtp.password", "hunter2")
    assert "hunter2" not in Credential.objects.get().ciphertext
    assert secrets.get("smtp.password") == "hunter2"
    secrets.clear("smtp.password")
    assert secrets.get("smtp.password") is None
