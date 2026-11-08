"""Refuse UPDATE on the audit log and lifecycle history at the database (FR-AUD-3).

The ORM already refuses; this holds for raw SQL and any future code path too.
DELETE stays possible so the retention purge can run.
"""

from django.db import migrations

TABLES = ["core_auditevent", "core_statechange"]

FUNCTION = """
CREATE OR REPLACE FUNCTION trammel_refuse_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
"""


def _create(table):
    return (
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION trammel_refuse_update();"
    )


def _drop(table):
    return f"DROP TRIGGER IF EXISTS {table}_no_update ON {table};"


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [
        migrations.RunSQL(FUNCTION, "DROP FUNCTION IF EXISTS trammel_refuse_update();"),
        *[migrations.RunSQL(_create(t), _drop(t)) for t in TABLES],
    ]
