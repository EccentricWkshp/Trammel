"""Invoice emails carry a link to pay online, when online payments are on (0.10.0).

Only texts still identical to the ones shipped before are replaced; edited
ones are the shop's own and are left alone.
"""

from django.db import migrations

OLD = {
    "invoice_issued": "Hello {{ customer.name }},\n\nPlease find {{ invoice.kind|lower }} {{ invoice.number }} attached for {{ invoice.total|money }}{% if invoice.due_date %}, due {{ invoice.due_date|date }}{% endif %}.\n\n{% if portal_link %}You can also see it online: {{ portal_link }}\n\n{% endif %}Thank you,\n{{ business.name }}\n",
    "invoice_overdue": "Hello {{ customer.name }},\n\nInvoice {{ invoice.number }} was due on {{ invoice.due_date|date }}. The balance is {{ invoice.balance|money }}.\n\nIf you have already paid, thank you, and please disregard this reminder.\n\n{{ business.name }}\n",
}


def forward(apps, schema_editor):
    from trammel.notifications.events import EVENTS

    EmailTemplate = apps.get_model("notifications", "EmailTemplate")
    for event, body in OLD.items():
        EmailTemplate.objects.filter(event=event, body=body).update(body=EVENTS[event].body)


class Migration(migrations.Migration):
    dependencies = [("notifications", "0004_portal_links")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
