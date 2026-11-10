"""Invoice lines billed from a change order now point at its quote line (0.4.3).

The change order's lines became quote lines with the same ids
(workorders 0007), so only the source type changes; nothing is billed twice.
"""

from django.db import migrations


def forward(apps, schema_editor):
    InvoiceLine = apps.get_model("billing", "InvoiceLine")
    InvoiceLine.objects.filter(source_type="change_order_line").update(source_type="quote_line")


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0007_template_archive"),
        ("workorders", "0007_change_orders_to_quotes"),
    ]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
