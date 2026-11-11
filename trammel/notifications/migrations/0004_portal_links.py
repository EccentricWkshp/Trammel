"""Quote and invoice emails link to the customer portal, when it's on (0.6.0).

Only texts still identical to the ones shipped before are replaced; edited
ones are the shop's own and are left alone.
"""

from django.db import migrations

OLD = {'quote_sent': 'Hello {{ customer.name }},\n\nPlease find {{ quote.document_title|lower }} {{ quote.number }} for "{{ quote.title }}" attached. The total is {{ quote.total|money }}{% if quote.valid_until %}, valid until {{ quote.valid_until|date }}{% endif %}.\n\nReply to this email with any questions.\n\n{{ business.name }}\n{{ business.phone }}\n', 'invoice_issued': 'Hello {{ customer.name }},\n\nPlease find {{ invoice.kind|lower }} {{ invoice.number }} attached for {{ invoice.total|money }}{% if invoice.due_date %}, due {{ invoice.due_date|date }}{% endif %}.\n\nThank you,\n{{ business.name }}\n'}


def forward(apps, schema_editor):
    from trammel.notifications.events import EVENTS

    EmailTemplate = apps.get_model("notifications", "EmailTemplate")
    for event, body in OLD.items():
        EmailTemplate.objects.filter(event=event, body=body).update(body=EVENTS[event].body)


class Migration(migrations.Migration):
    dependencies = [("notifications", "0003_change_order_wording")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
