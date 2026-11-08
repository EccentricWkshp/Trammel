"""Starting data for quotes: a default template (FR-QUO-5 "a default template ships")."""

from __future__ import annotations

DEFAULT_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Quote {{ quote.number }}</title>
<style>
  @page { size: letter; margin: 0.7in; }
  body { font-family: "Helvetica Neue", Arial, sans-serif; font-size: 10.5pt; color: #1d1f21;
         line-height: 1.4; margin: 0; padding: 1.5rem; }
  header { display: flex; justify-content: space-between; align-items: flex-start;
           border-bottom: 2px solid #1f5f8b; padding-bottom: 10pt; margin-bottom: 14pt; }
  h1 { font-size: 20pt; margin: 0; color: #1f5f8b; }
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
  .notes { margin-top: 18pt; white-space: pre-wrap; }
  .terms { margin-top: 24pt; font-size: 8.5pt; color: #333; white-space: pre-wrap;
           page-break-before: always; }
</style>
</head>
<body>
<header>
  <div>
    <h1>Quote</h1>
    <div>{{ quote.number }}{% if quote.revision > 1 %} (revision {{ quote.revision }}){% endif %}</div>
  </div>
  <div class="business">
    <strong>{{ business.name }}</strong><br>
    {% for line in business.address_lines %}{{ line }}<br>{% endfor %}
    {% if business.phone %}{{ business.phone }}<br>{% endif %}
    {% if business.email %}{{ business.email }}{% endif %}
  </div>
</header>

<div class="meta">
  <div>
    <h2>Prepared for</h2>
    <strong>{{ customer.name }}</strong><br>
    {% if customer.company %}{{ customer.company }}<br>{% endif %}
    {% for line in customer.address_lines %}{{ line }}<br>{% endfor %}
  </div>
  <div>
    <h2>Job</h2>
    {{ job.number }} — {{ quote.title }}<br>
    Date: {{ quote.date|date }}<br>
    {% if quote.valid_until %}Valid until: {{ quote.valid_until|date }}{% endif %}
  </div>
</div>

<table>
  <thead><tr><th>Description</th><th class="num">Qty</th><th class="num">Unit price</th><th class="num">Amount</th></tr></thead>
  <tbody>
  {% for line in quote.lines %}
    <tr>
      <td>{{ line.description }}{% if line.discount_pct %} <em>({{ line.discount_pct|percent }} off)</em>{% endif %}</td>
      <td class="num">{{ line.quantity }} {{ line.unit }}</td>
      <td class="num">{{ line.unit_price|money }}</td>
      <td class="num">{{ line.total|money }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>

<table class="totals">
  {% if quote.discount %}
  <tr><td>Subtotal</td><td class="num">{{ quote.lines_total|money }}</td></tr>
  <tr><td>Discount ({{ quote.discount_pct|percent }})</td><td class="num">−{{ quote.discount|money }}</td></tr>
  {% endif %}
  {% if quote.minimum_adjustment %}<tr><td>Minimum job charge</td><td class="num">{{ quote.minimum_adjustment|money }}</td></tr>{% endif %}
  {% if quote.shipping %}<tr><td>{{ quote.shipping_label }}</td><td class="num">{{ quote.shipping|money }}</td></tr>{% endif %}
  {% if quote.tax %}<tr><td>{{ quote.tax_label }}</td><td class="num">{{ quote.tax|money }}</td></tr>{% endif %}
  <tr class="grand"><td>Total</td><td class="num">{{ quote.total|money }}</td></tr>
  {% if quote.deposit %}<tr><td>Deposit due on approval</td><td class="num">{{ quote.deposit|money }}</td></tr>{% endif %}
</table>
{% if not quote.tax and quote.tax_label %}<p><small>{{ quote.tax_label }}.</small></p>{% endif %}

{% if quote.notes %}<div class="notes">{{ quote.notes }}</div>{% endif %}
{% if agreement %}<div class="terms">{{ agreement }}</div>{% endif %}
</body>
</html>
"""


def seed() -> None:
    from .models import QuoteTemplate

    if not QuoteTemplate.all_objects.exists():
        QuoteTemplate.objects.create(name="Standard quote", body=DEFAULT_TEMPLATE, is_default=True)
