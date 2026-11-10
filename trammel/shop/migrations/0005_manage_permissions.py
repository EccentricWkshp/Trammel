"""Machines, materials and vendors each get one permission (add, edit, archive
and delete). Roles and API keys that could edit them all keep that."""

from django.db import migrations

OLD = "shop.edit"
NEW = ["shop.manage_machines", "shop.manage_materials", "shop.manage_vendors"]


def _swap(values, old, new):
    if old not in values:
        return None
    return sorted((set(values) - {old}) | set(new))


def forward(apps, schema_editor):
    for label, field in (("accounts.Role", "permissions"), ("accounts.ApiKey", "scopes")):
        model = apps.get_model(label)
        for row in model.objects.filter(**{f"{field}__contains": [OLD]}):
            setattr(row, field, _swap(getattr(row, field), OLD, NEW))
            row.save(update_fields=[field])


def backward(apps, schema_editor):
    for label, field in (("accounts.Role", "permissions"), ("accounts.ApiKey", "scopes")):
        model = apps.get_model(label)
        for row in model.objects.filter(**{f"{field}__overlap": NEW}):
            values = set(getattr(row, field)) - set(NEW)
            setattr(row, field, sorted(values | {OLD}))
            row.save(update_fields=[field])


class Migration(migrations.Migration):
    dependencies = [
        ("shop", "0004_material_conversions"),
        ("accounts", "0004_home_sections"),
    ]

    operations = [migrations.RunPython(forward, backward)]
