"""Give the shipped invoice template the business logo and website.

Only a template still identical to the one 0.1.0 shipped is replaced; one that
has been edited is the shop's own and is left alone (its editor's placeholder
reference shows how to add the logo).
"""

import hashlib

from django.db import migrations

# SHA-256 of trammel.billing.seed.DEFAULT_TEMPLATE as shipped in 0.1.0.
SHIPPED_0_1_0 = "975fb54bb7c0fbed54184eac388b9fd823de736c54ef0b96f7aa0b87bc39438b"


def upgrade(apps, schema_editor):
    from trammel.billing.seed import DEFAULT_TEMPLATE

    InvoiceTemplate = apps.get_model("billing", "InvoiceTemplate")
    for template in InvoiceTemplate.objects.all():
        if hashlib.sha256(template.body.encode()).hexdigest() == SHIPPED_0_1_0:
            template.body = DEFAULT_TEMPLATE
            template.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("billing", "0005_alter_invoiceline_unit")]

    operations = [migrations.RunPython(upgrade, migrations.RunPython.noop)]
