"""Templates: a shipping line under the minimum charge.

Only standard-template text is matched, so customized templates keep their changes.
"""

from django.db import migrations

ANCHOR = "  {% if quote.tax %}"
ROW = (
    "  {% if quote.shipping %}<tr><td>{{ quote.shipping_label }}</td>"
    '<td class="num">{{ quote.shipping|money }}</td></tr>{% endif %}\n'
)


def forward(apps, schema_editor):
    QuoteTemplate = apps.get_model("quotes", "QuoteTemplate")
    for template in QuoteTemplate.objects.filter(body__contains=ANCHOR):
        if "quote.shipping" not in template.body:
            template.body = template.body.replace(ANCHOR, ROW + ANCHOR, 1)
            template.save(update_fields=["body"])


def backward(apps, schema_editor):
    QuoteTemplate = apps.get_model("quotes", "QuoteTemplate")
    for template in QuoteTemplate.objects.filter(body__contains=ROW):
        template.body = template.body.replace(ROW, "")
        template.save(update_fields=["body"])


class Migration(migrations.Migration):
    dependencies = [("quotes", "0002_shipping")]
    operations = [migrations.RunPython(forward, backward)]
