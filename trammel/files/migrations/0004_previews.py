"""Previews of DXF drawings, STL and 3MF models, and the thumbnails LightBurn and
G-code files carry (0.7.2, FR-DOC-4): files stored before now get them too, and
images and PDFs whose thumbnail failed are tried again."""

from django.db import migrations

KINDS = ("dxf", "stl", "3mf", "lightburn", "gcode")


def queue(apps, schema_editor):
    Blob = apps.get_model("files", "Blob")
    BackgroundTask = apps.get_model("core", "BackgroundTask")
    waiting = Blob.objects.filter(kind__in=KINDS, thumb_state="none") | Blob.objects.filter(
        kind__in=("image", "pdf"), thumb_state="failed"
    )
    ids = list(waiting.values_list("pk", flat=True))
    Blob.objects.filter(pk__in=ids).update(thumb_state="pending")
    BackgroundTask.objects.bulk_create(
        BackgroundTask(name="files.thumbnail", payload={"blob": str(pk)}) for pk in ids
    )


class Migration(migrations.Migration):
    dependencies = [
        ("files", "0003_shop_document"),
        ("core", "0015_shop_hours_by_day"),
    ]

    operations = [migrations.RunPython(queue, migrations.RunPython.noop)]
