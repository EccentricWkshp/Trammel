"""Quote documents show who approved them, and their signature (0.6.1).

Only a template still identical to the one shipped from 0.4.3 to 0.6.0 is
replaced; one that has been edited is the shop's own and is left alone.
"""

import hashlib

from django.db import migrations

# SHA-256 of trammel.quotes.seed.DEFAULT_TEMPLATE as shipped from 0.4.3 to 0.6.0.
SHIPPED_0_4_3 = "803a009141f45148f41493503721a6a610699255a17ef0dc760b4bfd879f5845"


def template(apps, schema_editor):
    from trammel.quotes.seed import DEFAULT_TEMPLATE

    QuoteTemplate = apps.get_model("quotes", "QuoteTemplate")
    for row in QuoteTemplate.objects.all():
        if hashlib.sha256(row.body.encode()).hexdigest() == SHIPPED_0_4_3:
            row.body = DEFAULT_TEMPLATE
            row.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("quotes", "0012_signatures")]

    operations = [migrations.RunPython(template, migrations.RunPython.noop)]
