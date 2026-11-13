"""The database password reaches PostgreSQL exactly as written (0.10.7): given
on its own (DATABASE_PASSWORD, as the Docker stack does), no character in it
is taken for URL syntax; inside DATABASE_URL it's URL-decoded, as before."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOW = (
    "import json, django; from django.conf import settings; "
    "d = settings.DATABASES['default']; "
    "print(json.dumps([d['USER'], d['PASSWORD'], d['HOST'], d['PORT'], d['NAME']]))"
)


def _settings(**env) -> list:
    keep = {k: v for k, v in os.environ.items() if not k.startswith(("DATABASE_", "DJANGO_"))}
    keep.update(SECRET_KEY="x" * 50, DJANGO_SETTINGS_MODULE="config.settings", **env)
    out = subprocess.run(  # noqa: S603 — this interpreter, a fixed script
        [sys.executable, "-c", SHOW],
        cwd=ROOT,
        env=keep,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_password_given_on_its_own_is_taken_as_it_is():
    tricky = "p%2Fa/b#c?d@e:f"
    assert _settings(
        DATABASE_URL="postgresql://trammel@db:5432/trammel", DATABASE_PASSWORD=tricky
    ) == ["trammel", tricky, "db", "5432", "trammel"]


def test_a_password_in_the_url_is_still_decoded():
    assert _settings(DATABASE_URL="postgresql://trammel:a%40b@db:5432/trammel")[1] == "a@b"
