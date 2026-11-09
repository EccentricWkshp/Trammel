"""Starting data for billing: a default invoice template (FR-BIL-3)."""

from __future__ import annotations

DEFAULT_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{{ invoice.kind }} {{ invoice.number }}</title>
<style>
  @page { size: letter; margin: 0.7in; }
  body { font-family: "Helvetica Neue", Arial, sans-serif; font-size: 10.5pt; color: #1d1f21;
         line-height: 1.4; margin: 0; padding: 1.5rem; }
  header { display: flex; justify-content: space-between; align-items: flex-start;
           border-bottom: 2px solid #1f5f8b; padding-bottom: 10pt; margin-bottom: 14pt; }
  h1 { font-size: 20pt; margin: 0; color: #1f5f8b; }
  .logo { display: block; max-height: 0.9in; max-width: 2.6in; margin-bottom: 8pt; }
  .business { text-align: right; font-size: 9.5pt; color: #444; }
  .meta { display: flex; justify-content: space-between; margin-bottom: 16pt; gap: 2rem; }
  .meta h2 { font-size: 9pt; text-transform: uppercase; letter-spacing: .05em; color: #666;
             margin: 0 0 3pt; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-size: 8.5pt; text-transform: uppercase; color: #666;
       border-bottom: 1px solid #999; padding: 4pt; }
  td { padding: 5pt 4pt; border-bottom: 1px solid #ddd; vertical-align: top; }
  .num { text-align: right; white-space: nowrap; }
  .totals { margin-left: auto; width: 45%; margin-top: 10pt; }
  .totals td { border: 0; padding: 2pt 4pt; }
  .totals .grand td { font-weight: bold; font-size: 12pt; border-top: 2px solid #1d1f21; }
  .stamp { display: inline-block; border: 2px solid #1f5126; color: #1f5126; padding: 2pt 8pt;
           font-weight: bold; text-transform: uppercase; }
  .notes { margin-top: 18pt; white-space: pre-wrap; }
  .regs { margin-top: 18pt; font-size: 8.5pt; color: #555; }
</style>
</head>
<body>
<header>
  <div>
    {% if business.logo %}<img class="logo" src="{{ business.logo }}" alt="{{ business.name }}">{% endif %}
    <h1>{{ invoice.kind }}</h1>
    <div>{{ invoice.number }}</div>
    {% if invoice.status == "Paid" %}<div class="stamp">Paid</div>{% endif %}
    {% if invoice.status == "Void" %}<div class="stamp">Void</div>{% endif %}
  </div>
  <div class="business">
    <strong>{{ business.name }}</strong><br>
    {% for line in business.address_lines %}{{ line }}<br>{% endfor %}
    {% if business.phone %}{{ business.phone }}<br>{% endif %}
    {% if business.email %}{{ business.email }}<br>{% endif %}
    {% if business.website %}{{ business.website }}{% endif %}
  </div>
</header>

<div class="meta">
  <div>
    <h2>Bill to</h2>
    <strong>{{ customer.name }}</strong><br>
    {% if customer.company %}{{ customer.company }}<br>{% endif %}
    {% for line in customer.address_lines %}{{ line }}<br>{% endfor %}
    {% if invoice.ship_to %}<h2 style="margin-top:8pt">Ship to</h2>{% for line in invoice.ship_to %}{{ line }}<br>{% endfor %}{% endif %}
  </div>
  <div>
    Date: {{ invoice.date|date }}<br>
    {% if invoice.due_date and not invoice.is_credit_note %}Due: {{ invoice.due_date|date }} ({{ invoice.terms_days }} days)<br>{% endif %}
    {% if invoice.job %}Job: {{ invoice.job }} — {{ invoice.job_title }}<br>{% endif %}
    {% if invoice.credit_for %}Credit against {{ invoice.credit_for }}{% endif %}
  </div>
</div>

<table>
  <thead><tr><th>Description</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Amount</th></tr></thead>
  <tbody>
  {% for line in invoice.lines %}
    <tr>
      <td>{{ line.description }}{% if line.discount_pct %} <em>({{ line.discount_pct|percent }} off)</em>{% endif %}{% if not line.taxable and invoice.taxes %} <small>(not taxed)</small>{% endif %}</td>
      <td class="num">{{ line.quantity }} {{ line.unit }}</td>
      <td class="num">{{ line.unit_price|money }}</td>
      <td class="num">{{ line.total|money }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>

<table class="totals">
  <tr><td>Subtotal</td><td class="num">{{ invoice.subtotal|money }}</td></tr>
  {% if invoice.shipping %}<tr><td>{{ invoice.shipping_label }}</td><td class="num">{{ invoice.shipping|money }}</td></tr>{% endif %}
  {% if invoice.tax_total %}<tr><td>{{ invoice.tax_label }}</td><td class="num">{{ invoice.tax_total|money }}</td></tr>{% endif %}
  {% if invoice.exemption_number %}<tr><td colspan="2"><small>Tax exempt — certificate {{ invoice.exemption_number }}</small></td></tr>{% endif %}
  <tr class="grand"><td>Total</td><td class="num">{{ invoice.total|money }}</td></tr>
  {% if invoice.paid %}<tr><td>Paid and credited</td><td class="num">−{{ invoice.paid|money }}</td></tr>{% endif %}
  {% if invoice.written_off %}<tr><td>Written off</td><td class="num">−{{ invoice.written_off|money }}</td></tr>{% endif %}
  {% if not invoice.is_credit_note %}<tr class="grand"><td>Balance due</td><td class="num">{{ invoice.balance|money }}</td></tr>{% endif %}
</table>

{% if invoice.notes %}<div class="notes">{{ invoice.notes }}</div>{% endif %}
{% if business.tax_registration_numbers %}<div class="regs">{% for r in business.tax_registration_numbers %}{{ r }}<br>{% endfor %}</div>{% endif %}
</body>
</html>
"""


def seed() -> None:
    from trammel.tax.models import TaxCategory

    from .models import InvoiceTemplate

    if not InvoiceTemplate.all_objects.exists():
        InvoiceTemplate.objects.create(
            name="Standard invoice", body=DEFAULT_TEMPLATE, is_default=True
        )
    TaxCategory.objects.get_or_create(name="Credit (not taxed)", defaults={"taxable": False})
