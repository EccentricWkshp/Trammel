"""Invoice emails list each way to pay online, and payment receipts say the paid
invoices are attached (0.10.1).

Only texts still identical to the ones shipped in 0.10.0 are replaced; edited
ones are the shop's own and are left alone.
"""

from django.db import migrations

OLD = {'payment_recorded': 'Hello {{ customer.name }},\n\nWe received your payment of {{ payment.amount|money }} on {{ payment.received_on|date }}.{% if credit %} {{ credit|money }} is held as credit on your account.{% endif %}\n\nThank you,\n{{ business.name }}\n', 'invoice_issued': 'Hello {{ customer.name }},\n\nPlease find {{ invoice.kind|lower }} {{ invoice.number }} attached for {{ invoice.total|money }}{% if invoice.due_date %}, due {{ invoice.due_date|date }}{% endif %}.\n\n{% if invoice.pay_url %}You can pay it online: {{ invoice.pay_url }}\n\n{% endif %}{% if portal_link %}You can also see it online: {{ portal_link }}\n\n{% endif %}Thank you,\n{{ business.name }}\n', 'invoice_overdue': 'Hello {{ customer.name }},\n\nInvoice {{ invoice.number }} was due on {{ invoice.due_date|date }}. The balance is {{ invoice.balance|money }}.\n\n{% if invoice.pay_url %}You can pay it online: {{ invoice.pay_url }}\n\n{% endif %}If you have already paid, thank you, and please disregard this reminder.\n\n{{ business.name }}\n'}


def forward(apps, schema_editor):
    from trammel.notifications.events import EVENTS

    EmailTemplate = apps.get_model("notifications", "EmailTemplate")
    for event, body in OLD.items():
        EmailTemplate.objects.filter(event=event, body=body).update(body=EVENTS[event].body)


class Migration(migrations.Migration):
    dependencies = [("notifications", "0005_pay_links")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
