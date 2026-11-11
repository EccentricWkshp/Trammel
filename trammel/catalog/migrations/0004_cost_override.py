"""A product's cost is calculated unless overridden (0.5.0).

Products whose cost was set from their bill of materials keep being worked
out, with the other costs recorded then. Every other product keeps the cost it
has as its override, so no product's cost changes on upgrade; clearing the
override has it calculated.
"""

from django.db import migrations


def forward(apps, schema_editor):
    Product = apps.get_model("catalog", "Product")
    ProductCosting = apps.get_model("stock", "ProductCosting")
    other = dict(ProductCosting.objects.values_list("product_id", "other"))
    for product in Product.objects.all():
        if product.pk in other:
            product.cost_override = None
            product.other_cost = other[product.pk]
        else:
            product.cost_override = product.cost
        product.save(update_fields=["cost_override", "other_cost"])


def backward(apps, schema_editor):
    Product = apps.get_model("catalog", "Product")
    for product in Product.objects.exclude(cost_override=None):
        product.cost = product.cost_override
        product.save(update_fields=["cost"])


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0003_routing"),
        ("stock", "0006_low_stock_notice"),
    ]

    operations = [migrations.RunPython(forward, backward)]
