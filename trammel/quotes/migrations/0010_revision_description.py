from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('quotes', '0009_change_order_template_and_permission'),
    ]

    operations = [
        migrations.AddField(
            model_name='quoterevision',
            name='description',
            field=models.TextField(blank=True, help_text="What is being done, in your own words: details the lines don't capture. Shown on the document above the lines."),
        ),
    ]
