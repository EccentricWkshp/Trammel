"""Shop hours by day (0.7.0): each working day keeps the one start and end the
shop had, and days off stay off."""

import datetime as dt

from django.db import migrations, models

import trammel.core.models


def by_day(apps, schema_editor):
    for profile in apps.get_model("core", "BusinessProfile").objects.all():
        shift = [profile.day_starts.strftime("%H:%M"), profile.day_ends.strftime("%H:%M")]
        profile.shop_hours = [shift if d in profile.work_days else None for d in range(7)]
        profile.save(update_fields=["shop_hours"])


def one_day(apps, schema_editor):
    """Back to one start and end: the first working day's."""
    for profile in apps.get_model("core", "BusinessProfile").objects.all():
        days = [d for d, shift in enumerate(profile.shop_hours or []) if shift]
        profile.work_days = days
        if days:
            start, end = profile.shop_hours[days[0]]
            profile.day_starts = dt.time.fromisoformat(start)
            profile.day_ends = dt.time.fromisoformat(end)
        profile.save(update_fields=["work_days", "day_starts", "day_ends"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_admin_mfa_help"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessprofile",
            name="shop_hours",
            field=models.JSONField(default=trammel.core.models.default_shop_hours),
        ),
        migrations.RunPython(by_day, one_day),
        migrations.RemoveField(model_name="businessprofile", name="work_days"),
        migrations.RemoveField(model_name="businessprofile", name="day_starts"),
        migrations.RemoveField(model_name="businessprofile", name="day_ends"),
    ]
