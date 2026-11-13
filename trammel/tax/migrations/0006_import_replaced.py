from django.db import migrations, models
from django.db.models import Exists, OuterRef


def tidy(apps, schema_editor):
    """What earlier imports left behind: replaced batches, and jurisdictions with no rate."""
    ImportBatch = apps.get_model("tax", "ImportBatch")
    Jurisdiction = apps.get_model("tax", "Jurisdiction")
    TaxRate = apps.get_model("tax", "TaxRate")
    Boundary = apps.get_model("tax", "Boundary")
    Invoice = apps.get_model("billing", "Invoice")
    ImportBatch.objects.filter(status="done", rows__gt=0).exclude(
        Exists(TaxRate.objects.filter(batch=OuterRef("pk")))
    ).exclude(Exists(Boundary.objects.filter(batch=OuterRef("pk")))).update(status="replaced")
    unused = (
        Jurisdiction.objects.exclude(source="manual")
        .exclude(Exists(TaxRate.objects.filter(jurisdiction=OuterRef("pk"))))
        .exclude(Exists(Invoice.objects.filter(manual_jurisdiction=OuterRef("pk"))))
    )
    Jurisdiction.objects.filter(pk__in=unused.values("pk")).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tax", "0005_wording"),
        ("billing", "0013_paid_stamp"),
    ]

    operations = [
        migrations.AlterField(
            model_name="importbatch",
            name="status",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("running", "Running"),
                    ("done", "Done"),
                    ("failed", "Failed"),
                    ("replaced", "Replaced"),
                ],
                default="done",
                max_length=10,
            ),
        ),
        migrations.RunPython(tidy, migrations.RunPython.noop),
    ]
