from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('quotes', '0007_template_archive'),
    ]

    operations = [
        migrations.AddField(
            model_name='quote',
            name='kind',
            field=models.CharField(choices=[('quote', 'Quote'), ('change_order', 'Change order')], db_index=True, default='quote', editable=False, max_length=12),
        ),
        migrations.AddField(
            model_name='quoterevision',
            name='amended_terms',
            field=models.TextField(blank=True, help_text='Changed terms the customer accepts with this change order (optional).'),
        ),
    ]
