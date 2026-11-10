"""Quote emails name a change order as one (0.4.3).

Only texts still identical to the ones shipped before are replaced; edited
ones are the shop's own and are left alone.
"""

from django.db import migrations

OLD = {'quote_sent': ('Quote {{ quote.number }} from {{ business.name }}', 'Hello {{ customer.name }},\n\nPlease find quote {{ quote.number }} for "{{ quote.title }}" attached. The total is {{ quote.total|money }}{% if quote.valid_until %}, valid until {{ quote.valid_until|date }}{% endif %}.\n\nReply to this email with any questions.\n\n{{ business.name }}\n{{ business.phone }}\n'), 'quote_approved': ('Approved: {{ quote.number }} ({{ customer.name }})', '{{ approved_by }} approved quote {{ quote.number }} revision {{ quote.revision }} for {{ customer.name }}: {{ quote.total|money }}.\n'), 'quote_declined': ('Declined: {{ quote.number }} ({{ customer.name }})', 'Quote {{ quote.number }} for {{ customer.name }} was declined.{% if reason %}\nReason: {{ reason }}{% endif %}\n')}


def forward(apps, schema_editor):
    from trammel.notifications.events import EVENTS

    EmailTemplate = apps.get_model("notifications", "EmailTemplate")
    for event, (subject, body) in OLD.items():
        spec = EVENTS[event]
        EmailTemplate.objects.filter(event=event, subject=subject).update(subject=spec.subject)
        EmailTemplate.objects.filter(event=event, body=body).update(body=spec.body)


class Migration(migrations.Migration):
    dependencies = [("notifications", "0002_staff_recipient_users")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
