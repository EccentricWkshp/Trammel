"""Templates: one combined sales tax line, and a shipping line.

Only the rows written by the standard template are replaced, so a template
that was customized elsewhere keeps its changes.
"""

from django.db import migrations

OLD = (
    "  {% for t in invoice.taxes %}<tr><td>{{ t.name }} ({{ t.rate|percent }})</td>"
    '<td class="num">{{ t.amount|money }}</td></tr>{% endfor %}'
)
NEW = (
    "  {% if invoice.shipping %}<tr><td>{{ invoice.shipping_label }}</td>"
    '<td class="num">{{ invoice.shipping|money }}</td></tr>{% endif %}\n'
    "  {% if invoice.tax_total %}<tr><td>{{ invoice.tax_label }}</td>"
    '<td class="num">{{ invoice.tax_total|money }}</td></tr>{% endif %}'
)


def forward(apps, schema_editor):
    InvoiceTemplate = apps.get_model("billing", "InvoiceTemplate")
    for template in InvoiceTemplate.objects.filter(body__contains=OLD):
        template.body = template.body.replace(OLD, NEW)
        template.save(update_fields=["body"])


def backward(apps, schema_editor):
    InvoiceTemplate = apps.get_model("billing", "InvoiceTemplate")
    for template in InvoiceTemplate.objects.filter(body__contains=NEW):
        template.body = template.body.replace(NEW, OLD)
        template.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("billing", "0003_shipping")]
    operations = [migrations.RunPython(forward, backward)]
