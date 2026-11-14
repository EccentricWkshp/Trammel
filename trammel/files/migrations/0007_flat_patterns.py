"""Flat patterns of sheet metal parts (0.11.2, FR-DOC-4): STEP files stored
before now are read again, so a sheet metal part gets its flat pattern."""

import django.db.models.deletion
from django.db import migrations, models


def queue(apps, schema_editor):
    Blob = apps.get_model("files", "Blob")
    BackgroundTask = apps.get_model("core", "BackgroundTask")
    ids = list(Blob.objects.filter(kind="step").values_list("pk", flat=True))
    Blob.objects.filter(pk__in=ids).update(thumb_state="pending")
    BackgroundTask.objects.bulk_create(
        BackgroundTask(name="files.thumbnail", payload={"blob": str(pk)}) for pk in ids
    )


class Migration(migrations.Migration):
    dependencies = [
        ("files", "0006_step_previews"),
        ("shop", "0008_material_k_factor"),
        ("core", "0015_shop_hours_by_day"),
    ]

    operations = [
        migrations.AddField(
            model_name="blob",
            name="flat",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="document",
            name="sheet_material",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="shop.material",
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="flat_on_traveler",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(queue, migrations.RunPython.noop),
    ]
