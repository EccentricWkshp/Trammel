"""Untrusted work in a child process with memory and time limits (SEC-02,
SEC-09, 0.10.6). The process tests run where fork and resource limits exist
(Linux, as in Docker and CI); the output limit holds everywhere."""

import pytest

from trammel.core import limits, templating

linux = pytest.mark.skipif(not limits.available(), reason="needs fork and resource limits")


def _boom():
    raise ValueError("bad drawing")


def _hog():
    return bytearray(2 * 1024 * 1024 * 1024)


def _spin():
    while True:
        pass


def _query():
    from trammel.customers.models import Customer

    return Customer.objects.count()


@linux
def test_a_result_and_an_error_come_back():
    assert limits.run(sorted, [3, 1, 2], memory_mb=64, seconds=5) == [1, 2, 3]
    with pytest.raises(ValueError, match="bad drawing"):
        limits.run(_boom, memory_mb=64, seconds=5)


@linux
def test_too_much_memory_or_time_fails_alone():
    with pytest.raises(limits.Exceeded) as hog:
        limits.run(_hog, memory_mb=64, seconds=5)
    assert hog.value.what == "memory"
    with pytest.raises(limits.Exceeded) as spin:
        limits.run(_spin, memory_mb=64, seconds=1)
    assert spin.value.what == "time"


@linux
@pytest.mark.django_db
def test_the_child_has_no_database_and_the_parent_keeps_its_own():
    from trammel.customers.models import Customer

    with pytest.raises(limits.NeedsDatabase):
        limits.run(_query, memory_mb=128, seconds=5)
    assert Customer.objects.count() >= 0  # this connection still works


def test_a_template_can_only_make_so_much_text():
    with pytest.raises(templating.TemplateProblem, match="too much text"):
        templating.render("{{ range(1000) | join('x' * 10000) }}", {}, html=False)
    assert templating.render("{{ 'ok' }}", {}, html=False) == "ok"


@linux
def test_a_template_that_doubles_text_runs_out_of_room():
    source = (
        "{% set ns = namespace(s='x' * 1000) %}"
        "{% for i in range(40) %}{% set ns.s = ns.s ~ ns.s %}{% endfor %}{{ ns.s | length }}"
    )
    with pytest.raises(templating.TemplateProblem, match="memory|too long"):
        templating.render(source, {}, html=False)
