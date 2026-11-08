"""Superseded by trammel/agreements/seed.py, which runs after every migrate.

Kept so the migration graph stays the same for databases that applied it.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("agreements", "0001_initial")]
    operations = []
