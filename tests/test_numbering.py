"""SPEC §4.4: readable document numbers, gap-free."""

import datetime as dt

import pytest
from django.db import transaction

from trammel.core.models import DocumentSequence
from trammel.core.numbering import NumberingError, next_number, preview_next


@pytest.mark.django_db
def test_yearly_series_formats_and_restarts_each_year(setup_done):
    with transaction.atomic():
        assert next_number("widget", dt.date(2026, 3, 1)) == "W-2026-0001"
        assert next_number("widget", dt.date(2026, 3, 2)) == "W-2026-0002"
        assert next_number("widget", dt.date(2027, 1, 1)) == "W-2027-0001"


@pytest.mark.django_db
def test_non_yearly_series(setup_done):
    with transaction.atomic():
        assert next_number("ticket") == "T-001"
        assert next_number("ticket") == "T-002"


@pytest.mark.django_db
def test_prefix_change_applies_to_new_numbers(setup_done):
    DocumentSequence.objects.filter(key="ticket").update(prefix="TK")
    with transaction.atomic():
        assert next_number("ticket") == "TK-001"


@pytest.mark.django_db(transaction=True)  # no wrapping test transaction
def test_allocation_outside_a_transaction_is_refused(setup_done):
    with pytest.raises(NumberingError):
        next_number("ticket")


@pytest.mark.django_db
def test_rollback_leaves_no_gap(setup_done):
    class Boom(Exception):
        pass

    with transaction.atomic():
        next_number("ticket")
    with pytest.raises(Boom), transaction.atomic():
        next_number("ticket")
        raise Boom
    assert preview_next("ticket") == "T-002"
    with transaction.atomic():
        assert next_number("ticket") == "T-002"
