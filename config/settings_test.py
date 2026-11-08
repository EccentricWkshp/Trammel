"""Settings for the test suite: the real settings plus a test-only module."""

import os

os.environ.setdefault("DEBUG", "true")

from .settings import *  # noqa: E402, F403
from .settings import INSTALLED_APPS  # noqa: E402

INSTALLED_APPS = [*INSTALLED_APPS, "tests.testapp.apps.TestAppConfig"]

# Argon2 is deliberately slow; the suite creates many users. Test-only.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Uploaded files go to a throwaway directory, never the developer's data/.
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="trammel-test-media-"))
BACKUP_DIR = Path(tempfile.mkdtemp(prefix="trammel-test-backups-"))

# Email goes to django.core.mail.outbox instead of an SMTP server.
TRAMMEL_EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
