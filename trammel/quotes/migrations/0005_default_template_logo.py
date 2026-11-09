"""Give the shipped quote template the business logo and website.

Only a template still identical to the one 0.1.0 shipped is replaced; one that
has been edited is the shop's own and is left alone (its editor's placeholder
reference shows how to add the logo).
"""

import hashlib

from django.db import migrations

# SHA-256 of trammel.quotes.seed.DEFAULT_TEMPLATE as shipped in 0.1.0.
SHIPPED_0_1_0 = "8f9ee4fc1555d7ccc54f10d095a1368e7dc488dd52bc0603dde12270d9380373"


def upgrade(apps, schema_editor):
    from trammel.quotes.seed import DEFAULT_TEMPLATE

    QuoteTemplate = apps.get_model("quotes", "QuoteTemplate")
    for template in QuoteTemplate.objects.all():
        if hashlib.sha256(template.body.encode()).hexdigest() == SHIPPED_0_1_0:
            template.body = DEFAULT_TEMPLATE
            template.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("quotes", "0004_alter_quoteline_unit")]

    operations = [migrations.RunPython(upgrade, migrations.RunPython.noop)]
