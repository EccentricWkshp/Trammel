from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('workorders', '0007_change_orders_to_quotes'),
        ('billing', '0008_change_order_lines'),
    ]

    operations = [
        migrations.DeleteModel(
            name='ChangeOrderLine',
        ),
        migrations.DeleteModel(
            name='ChangeOrder',
        ),
    ]
