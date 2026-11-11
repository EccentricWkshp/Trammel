"""Shop hours by day (0.7.0): each day its own hours, weekends and night shifts
included, in the estimates, the settings page and the time report."""

import datetime as dt
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from trammel.core.models import BusinessProfile
from trammel.core.workhours import Hours, shifts_from
from trammel.reports import services as reports

D = Decimal
UTC = dt.UTC
T = dt.time
URL = "/settings/business/?tab=hours"


def at(day, hour, minute=0):
    """A moment in November 2026: the 13th is a Friday, the 16th a Monday."""
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def week(**days):
    """Hours with the given days' (start, end), by the day's first three letters."""
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return Hours(tuple(days.get(n) for n in names), UTC)


def test_a_night_shift_runs_past_midnight_and_counts_as_the_day_it_starts():
    nights = week(**{d: (T(22), T(6)) for d in ("mon", "tue", "wed", "thu")})
    # Monday 11 PM: 7 hours to 6 AM, then the other 3 from Tuesday at 10 PM.
    finish = nights.finish(at(16, 23), [(D("600"), D("0"))])
    assert finish == at(18, 1)
    assert nights.working_days_between(at(16, 23), finish) == 2
    # Tuesday at 3 AM is still Monday's shift; Friday at 3 AM is Thursday's.
    assert nights.finish(at(17, 3), [(D("60"), D("0"))]) == at(17, 4)
    # 3 hours to Friday 6 AM, then the last one Monday 10 to 11 PM.
    assert nights.finish(at(20, 3), [(D("240"), D("0"))]) == at(23, 23)
    assert nights.first_working_moment(dt.date(2026, 11, 17)) == at(17, 22)


def test_saturday_keeps_its_own_hours():
    shop = week(
        mon=(T(8), T(16)),
        tue=(T(8), T(16)),
        wed=(T(8), T(16)),
        thu=(T(8), T(16)),
        fri=(T(8), T(16)),
        sat=(T(9), T(13)),
    )
    # Friday 2 PM: 2 hours Friday, 4 on Saturday morning, the last on Monday.
    assert shop.finish(at(13, 14), [(D("420"), D("0"))]) == at(16, 9)
    assert shop.minutes_on(dt.date(2026, 11, 14)) == 240
    assert shop.minutes_on(dt.date(2026, 11, 15)) == 0
    assert shop.available_minutes(dt.date(2026, 11, 15), dt.date(2026, 11, 21)) == 44 * 60


def test_the_same_start_and_end_works_around_the_clock():
    always = Hours.same_days(range(7), T(0), T(0))
    assert always.finish(at(16, 10), [(D("1800"), D("0"))]) == at(17, 16)
    assert always.available_minutes(dt.date(2026, 11, 16), dt.date(2026, 11, 16)) == 24 * 60
    # No hours at all reads the same way, rather than never finishing.
    assert Hours((None,) * 7, UTC).finish(at(16, 10), [(D("60"), D("0"))]) == at(16, 11)


def test_unreadable_stored_hours_count_as_days_off():
    assert shifts_from([["08:00", "16:00"], None, ["late"], "x"]) == (
        (T(8), T(16)),
        None,
        None,
        None,
        None,
        None,
        None,
    )


def _post(client, days=None):
    """The hours tab's form, with the given days' (start, end) by Python weekday,
    and the other days' time boxes sent blank, as a browser does."""
    days = days or {}
    data = {}
    for d in range(7):
        data[f"day{d}_starts"], data[f"day{d}_ends"] = days.get(d, ("", ""))
        if d in days:
            data[f"day{d}_works"] = "on"
    return client.post(URL, data)


@pytest.mark.django_db
def test_each_day_is_set_on_the_shop_hours_tab(admin_client):
    page = admin_client.get(URL).content.decode()
    # Listed from the day the week starts on: Sunday, by default.
    assert page.index("Sunday") < page.index("Monday") < page.index("Saturday")
    assert "8 h" in page and "Off" in page

    nights = {d: ("22:00", "06:00") for d in range(4)}
    _post(admin_client, {**nights, 4: ("07:00", "15:30"), 5: ("09:00", "13:00")})
    profile = BusinessProfile.load()
    assert profile.shop_hours == [["22:00", "06:00"]] * 4 + [
        ["07:00", "15:30"],
        ["09:00", "13:00"],
        None,
    ]
    page = admin_client.get(URL).content.decode()
    assert "8 h, past midnight" in page and "8 h 30 min" in page and "4 h" in page
    assert Hours.load().minutes_on(dt.date(2026, 11, 14)) == 240


@pytest.mark.django_db
def test_hours_that_dont_make_sense_are_refused(admin_client):
    before = BusinessProfile.load().shop_hours
    # Thursday's night shift runs to 6 AM; Friday can't start at 5.
    page = _post(admin_client, {3: ("22:00", "06:00"), 4: ("05:00", "13:00")})
    assert "Friday can&#x27;t start before Thursday&#x27;s hours end." in page.content.decode()
    page = _post(admin_client, {1: ("08:00", "")})
    assert "Give the time Tuesday ends." in page.content.decode()
    page = _post(admin_client)
    assert "Pick at least one working day." in page.content.decode()
    assert BusinessProfile.load().shop_hours == before
    # Sunday's night shift ending at 6 AM leaves Monday free to start then.
    _post(admin_client, {6: ("22:00", "06:00"), 0: ("06:00", "14:00")})
    assert BusinessProfile.load().shop_hours[0] == ["06:00", "14:00"]


@pytest.mark.django_db
def test_the_time_report_measures_machine_use_against_each_days_hours(admin_client):
    profile = BusinessProfile.load()
    profile.shop_hours = [["08:00", "16:00"]] * 5 + [["09:00", "13:00"], None]
    profile.save()
    # Sunday the 15th through Saturday the 21st: five 8-hour days and a 4-hour one.
    _, available = reports.time_report(dt.date(2026, 11, 15), dt.date(2026, 11, 21), "machine")
    assert available == D("44")
    page = admin_client.get(
        "/reports/time/?group=machine&start=2026-11-15&end=2026-11-21"
    ).content.decode()
    assert "over 44 available hours" in page


# Upgrading from 0.6.x


BEFORE = [("core", "0014_admin_mfa_help")]


def _migrate(targets):
    MigrationExecutor(connection).migrate(targets)
    return MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps


@pytest.mark.django_db(transaction=True)
def test_the_working_days_keep_their_hours_on_upgrade():
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        apps = _migrate(BEFORE)
        Profile = apps.get_model("core", "BusinessProfile")
        Profile.objects.update_or_create(
            pk=1, defaults={"work_days": [1, 2, 3, 4, 5], "day_starts": T(7), "day_ends": T(17, 30)}
        )
        apps = _migrate(latest)
        hours = apps.get_model("core", "BusinessProfile").objects.get(pk=1).shop_hours
        assert hours == [None] + [["07:00", "17:30"]] * 5 + [None]
    finally:
        _migrate(latest)
