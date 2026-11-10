import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('quotes', '0008_change_orders'),
        ('workorders', '0005_planned_operation'),
    ]

    operations = [
        migrations.AddField(
            model_name='operation',
            name='source_revision',
            field=models.ForeignKey(blank=True, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='quotes.quoterevision'),
        ),
    ]
