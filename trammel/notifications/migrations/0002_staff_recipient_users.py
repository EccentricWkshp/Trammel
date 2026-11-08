from django.conf import settings
from django.db import migrations, models


def addresses_to_users(apps, schema_editor):
    """Each listed address that belongs to a user becomes that user, chosen."""
    SmtpSettings = apps.get_model("notifications", "SmtpSettings")
    User = apps.get_model("accounts", "User")
    for row in SmtpSettings.objects.all():
        addresses = [a.strip().lower() for a in row.staff_recipients.splitlines() if a.strip()]
        if addresses:
            row.staff_users.set(User.objects.filter(email__in=addresses))


def users_to_addresses(apps, schema_editor):
    SmtpSettings = apps.get_model("notifications", "SmtpSettings")
    for row in SmtpSettings.objects.all():
        row.staff_recipients = "\n".join(row.staff_users.values_list("email", flat=True))
        row.save(update_fields=["staff_recipients"])


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0001_initial"),
        ("accounts", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="smtpsettings",
            name="staff_users",
            field=models.ManyToManyField(blank=True, related_name="+", to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(addresses_to_users, users_to_addresses),
        migrations.RemoveField(model_name="smtpsettings", name="staff_recipients"),
        migrations.RenameField(
            model_name="smtpsettings", old_name="staff_users", new_name="staff_recipients"
        ),
        migrations.AlterField(
            model_name="smtpsettings",
            name="staff_recipients",
            field=models.ManyToManyField(
                blank=True,
                help_text="Who gets staff notifications. Nobody chosen sends them to every "
                "system admin.",
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
