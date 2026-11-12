"""The default invoice template marks a paid invoice clearly (a large stamp with
the date paid, or "Partly paid" with what's still due) and lists the payments
received (0.10.1).

Only a template still identical to one shipped before is replaced (recognized
by its SHA-256); an edited one is the shop's own and is left alone.
"""

import hashlib

from django.db import migrations

#: The default template as shipped in 0.1.x, and from the business logo on.
SHIPPED = {
    "975fb54bb7c0fbed54184eac388b9fd823de736c54ef0b96f7aa0b87bc39438b",
    "c61a25041b459ec5e5865fcece8ccdc0ac50048a238994b0906bfa8d77d85b11",
}


def forward(apps, schema_editor):
    from trammel.billing.seed import DEFAULT_TEMPLATE

    InvoiceTemplate = apps.get_model("billing", "InvoiceTemplate")
    for row in InvoiceTemplate.objects.all():
        if hashlib.sha256(row.body.encode()).hexdigest() in SHIPPED:
            InvoiceTemplate.objects.filter(pk=row.pk).update(body=DEFAULT_TEMPLATE)


class Migration(migrations.Migration):
    dependencies = [("billing", "0012_alter_payment_method")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
