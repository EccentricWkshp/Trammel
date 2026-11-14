"""STEP previews (0.11.1, FR-DOC-4): STEP files stored before now are read for
their picture, measurements and 3D viewer mesh."""

from django.db import migrations


def queue(apps, schema_editor):
    Blob = apps.get_model("files", "Blob")
    BackgroundTask = apps.get_model("core", "BackgroundTask")
    ids = list(
        Blob.objects.filter(kind="step", thumb_state__in=("none", "failed")).values_list(
            "pk", flat=True
        )
    )
    Blob.objects.filter(pk__in=ids).update(thumb_state="pending")
    BackgroundTask.objects.bulk_create(
        BackgroundTask(name="files.thumbnail", payload={"blob": str(pk)}) for pk in ids
    )


class Migration(migrations.Migration):
    dependencies = [
        ("files", "0005_model_viewer"),
        ("core", "0015_shop_hours_by_day"),
    ]

    operations = [migrations.RunPython(queue, migrations.RunPython.noop)]
