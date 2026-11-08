"""Date and time display follows the business format and the active time zone."""

import datetime as dt
import zoneinfo

from django.template import Context, Template
from django.utils import timezone

from trammel.core import formats


def test_times_show_in_the_active_time_zone():
    moment = dt.datetime(2026, 11, 8, 5, 28, tzinfo=dt.UTC)
    with timezone.override(zoneinfo.ZoneInfo("America/Los_Angeles")):
        rendered = Template("{{ v|dt }}|{{ v|t }}|{{ v|mdt }}").render(Context({"v": moment}))
        assert rendered == "11/07/2026 9:28 PM|9:28 PM|11/7 9:28 PM"
        assert formats.show(moment, "d") == "11/07/2026"


def test_plain_dates_are_not_shifted():
    with timezone.override(zoneinfo.ZoneInfo("Pacific/Kiritimati")):
        assert Template("{{ v|d }}").render(Context({"v": dt.date(2026, 11, 7)})) == "11/07/2026"
