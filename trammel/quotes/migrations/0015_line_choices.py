"""Optional and alternate quote lines (0.7.1, FR-QUO-11).

The default template gains the options and alternatives left out of the total.
Only a template still identical to the one shipped from 0.6.1 to 0.7.0 is
replaced; one that has been edited is the shop's own and is left alone.
"""

import hashlib

from django.db import migrations, models

# SHA-256 of trammel.quotes.seed.DEFAULT_TEMPLATE as shipped from 0.6.1 to 0.7.0.
SHIPPED_0_6_1 = "2c3220bd9f9135fd8bc0a9da71438e89537dc3b0172066fb0d457b6d6a0e1105"


def template(apps, schema_editor):
    from trammel.quotes.seed import DEFAULT_TEMPLATE

    QuoteTemplate = apps.get_model("quotes", "QuoteTemplate")
    for row in QuoteTemplate.objects.all():
        if hashlib.sha256(row.body.encode()).hexdigest() == SHIPPED_0_6_1:
            row.body = DEFAULT_TEMPLATE
            row.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [
        ("quotes", "0014_paid_online"),
    ]

    operations = [
        migrations.RemoveField(model_name="quoteline", name="is_optional"),
        migrations.AddField(
            model_name="quoteline",
            name="group",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="quoteline",
            name="included",
            field=models.BooleanField(default=True, editable=False),
        ),
        migrations.AddField(
            model_name="quoteline",
            name="offer",
            field=models.CharField(
                choices=[
                    ("included", "Included"),
                    ("optional", "Optional: the customer may add it"),
                    ("alternate", "An alternative: the customer picks one of a group"),
                ],
                default="included",
                max_length=10,
            ),
        ),
        migrations.RunPython(template, migrations.RunPython.noop),
    ]
