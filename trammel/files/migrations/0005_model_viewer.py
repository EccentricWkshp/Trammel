"""The 3D viewer (0.11.0, FR-DOC-4): a model's mesh for the viewer and its
measurements. STL and 3MF files stored before now are read again for them, and
3MF pictures redrawn with each part where its build puts it."""

from django.db import migrations, models

KINDS = ("stl", "3mf")


def queue(apps, schema_editor):
    Blob = apps.get_model("files", "Blob")
    BackgroundTask = apps.get_model("core", "BackgroundTask")
    ids = list(Blob.objects.filter(kind__in=KINDS).values_list("pk", flat=True))
    Blob.objects.filter(pk__in=ids).update(thumb_state="pending")
    BackgroundTask.objects.bulk_create(
        BackgroundTask(name="files.thumbnail", payload={"blob": str(pk)}) for pk in ids
    )


class Migration(migrations.Migration):
    dependencies = [
        ("files", "0004_previews"),
        ("core", "0015_shop_hours_by_day"),
    ]

    operations = [
        migrations.AddField(
            model_name="blob",
            name="mesh",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="blob",
            name="model",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.RunPython(queue, migrations.RunPython.noop),
    ]
