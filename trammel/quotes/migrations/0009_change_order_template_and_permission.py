"""Change orders are quotes of their own kind (0.4.3).

* The shipped quote template names the document: “Quote” or “Change order”.
  Only a template still identical to the one shipped since 0.1.1 is replaced;
  one that has been edited is the shop's own and is left alone.
* Recording change order approval moves from work orders to quotes. Roles
  and API keys that could approve change orders still can.
"""

import hashlib

from django.db import migrations

# SHA-256 of trammel.quotes.seed.DEFAULT_TEMPLATE as shipped from 0.1.1 to 0.4.2.
SHIPPED_0_1_1 = "cdd1ea027ff89e2216e345c6cb014f803f48c76bef39f9254a6609b5cb860215"

OLD = "workorders.approve"
NEW = "quotes.approve_change_orders"
HOLDERS = (("accounts.Role", "permissions"), ("accounts.ApiKey", "scopes"))


def template(apps, schema_editor):
    from trammel.quotes.seed import DEFAULT_TEMPLATE

    QuoteTemplate = apps.get_model("quotes", "QuoteTemplate")
    for row in QuoteTemplate.objects.all():
        if hashlib.sha256(row.body.encode()).hexdigest() == SHIPPED_0_1_1:
            row.body = DEFAULT_TEMPLATE
            row.save(update_fields=["body"])


def _swap(apps, old, new):
    for label, field in HOLDERS:
        model = apps.get_model(label)
        for row in model.objects.filter(**{f"{field}__contains": [old]}):
            setattr(row, field, sorted((set(getattr(row, field)) - {old}) | {new}))
            row.save(update_fields=[field])


def permission(apps, schema_editor):
    _swap(apps, OLD, NEW)


def permission_back(apps, schema_editor):
    _swap(apps, NEW, OLD)


class Migration(migrations.Migration):
    dependencies = [
        ("quotes", "0008_change_orders"),
        ("accounts", "0005_preferences"),
    ]

    operations = [
        migrations.RunPython(template, migrations.RunPython.noop),
        migrations.RunPython(permission, permission_back),
    ]
