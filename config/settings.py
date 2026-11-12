"""
Trammel settings.

Only infrastructure is configured from the environment: what the process needs
before it can read its own database. Business configuration (name, currency,
rates, document prefixes, enabled modules) lives in the database and is edited
in the application (FR-ADM-1..4, FR-ADM-7).
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP
from dotenv import load_dotenv

from trammel import __version__ as _app_version

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(key: str, default: str = "") -> str:
    """Read a setting, supporting `<KEY>_FILE` indirection for Docker secrets."""
    if path := os.environ.get(f"{key}_FILE"):
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    return env(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key, str(default)))
    except ValueError:
        return default


def env_list(key: str, default: str = "") -> list[str]:
    return [v.strip() for v in env(key, default).split(",") if v.strip()]


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

APP_VERSION = _app_version
APP_REVISION = env("APP_REVISION", "")

DEBUG = env_bool("DEBUG", False)

# No default credentials ship (SPEC §9). A development key is allowed only with
# DEBUG on, so a production instance cannot boot on a key published here.
SECRET_KEY = env("SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("SECRET_KEY must be set (see .env.example).")
    SECRET_KEY = "dev-only-insecure-key-not-for-production"  # noqa: S105

CREDENTIAL_KEY = env("CREDENTIAL_KEY", "")

BASE_URL = env("BASE_URL", "http://localhost:8000").rstrip("/")
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")
CSRF_TRUSTED_ORIGINS = [
    o for o in env_list("CSRF_TRUSTED_ORIGINS", BASE_URL) if o.startswith("http")
]

# Reverse proxies whose X-Forwarded-For is believed for the client address
# (audit log, login rate limits): comma-separated addresses or networks, such
# as 192.168.1.20 or 10.0.0.0/8. "*" believes any sender, which is right only
# when nothing but the proxy can reach the app, as with the Compose stack's
# built-in proxy; anywhere else a client could forge its own address. Empty
# means the connecting address is the client.
TRUSTED_PROXIES = env_list("TRUSTED_PROXIES", "")
for _proxy in TRUSTED_PROXIES:
    if _proxy != "*":
        try:
            ipaddress.ip_network(_proxy, strict=False)
        except ValueError:
            raise ImproperlyConfigured(
                f"TRUSTED_PROXIES: {_proxy!r} is not an IP address or network."
            ) from None

# Optional one-time token required by the setup page. Set it when a fresh
# instance might be reachable by someone other than you before setup is done.
SETUP_TOKEN = env("SETUP_TOKEN", "")

# Forces the customer portal off, whatever Settings says, until it is removed:
# every portal address answers "not found" and no sign-in code is sent. For
# shutting the portal at once, even without signing in (FR-PRT-8).
PORTAL_OFF = env_bool("PORTAL_OFF", False)

# The Shopify Admin API version the order importer asks for. Shopify supports
# each for a year; Trammel releases move it on.
SHOPIFY_API_VERSION = env("SHOPIFY_API_VERSION", "2026-01")

# API rate limit per authenticated principal (FR-API-3), e.g. "120/m".
API_RATE_LIMIT = env("API_RATE_LIMIT", "120/m")

# The cache lives in PostgreSQL (SPEC §3.5: no Redis). Used for API throttling.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "trammel_cache",
    }
}

# One switch for every outbound connection (SPEC §3.1). The setting in the
# database can also turn it on; this forces it regardless.
OFFLINE_MODE = env_bool("OFFLINE_MODE", False)

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    # Under runserver too, WhiteNoise serves static files (not Django's own
    # view), with a Cache-Control header: browsers check for a newer script or
    # stylesheet instead of guessing from the file's age.
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "django.forms",  # widget templates for the form renderer
    # The API docs page's own copy of Swagger UI. Without the app installed,
    # Ninja loads it from a CDN, which the content security policy blocks.
    "ninja",
    # Core modules (SPEC §4.5) — cannot be disabled.
    "trammel.core.apps.CoreConfig",
    "trammel.accounts.apps.AccountsConfig",
    "trammel.customers.apps.CustomersConfig",
    "trammel.system.apps.SystemConfig",
    # Optional modules — listed by config class (see docs/DEVELOPMENT.md).
    "trammel.files.apps.FilesConfig",
    "trammel.shop.apps.ShopConfig",
    "trammel.tax.apps.TaxConfig",
    "trammel.catalog.apps.CatalogConfig",
    "trammel.jobs.apps.JobsConfig",
    "trammel.agreements.apps.AgreementsConfig",
    "trammel.quotes.apps.QuotesConfig",
    "trammel.workorders.apps.WorkOrdersConfig",
    "trammel.stock.apps.StockConfig",
    "trammel.purchasing.apps.PurchasingConfig",
    "trammel.timetracking.apps.TimeTrackingConfig",
    "trammel.floor.apps.FloorConfig",
    "trammel.billing.apps.BillingConfig",
    "trammel.reports.apps.ReportsConfig",
    "trammel.notifications.apps.NotificationsConfig",
    "trammel.portal.apps.PortalConfig",
    "trammel.edge.apps.EdgeConfig",
    "trammel.imports.apps.ImportsConfig",
    "trammel.api",
]

MIDDLEWARE = [
    # The container health check, answered before the Host header is checked or
    # plain HTTP is redirected: it calls 127.0.0.1, which ALLOWED_HOSTS needn't list.
    "trammel.core.middleware.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Static files from the app process: gunicorn serves none and Caddy only proxies.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Users whose role requires two-factor sign-in must set it up first (SPEC §9).
    "trammel.accounts.middleware.MfaEnrollmentMiddleware",
    # Signs people out a set number of days after they signed in (SPEC §9).
    "trammel.accounts.middleware.SessionLifetimeMiddleware",
    # Sends every request to the setup page until an admin exists (FR-ADM-1).
    "trammel.core.middleware.SetupRequiredMiddleware",
    # Binds the acting principal and source IP for the audit log (FR-AUD-1).
    "trammel.core.middleware.AuditContextMiddleware",
    # Applies the per-user timezone, falling back to the business timezone.
    "trammel.core.middleware.TimezoneMiddleware",
]

ROOT_URLCONF = "config.urls"
FORM_RENDERER = "trammel.core.renderers.TrammelFormRenderer"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "trammel.core.context.instance",
            ],
            "builtins": ["trammel.core.templatetags.trammel"],
        },
    },
]

# --------------------------------------------------------------------------
# Database — PostgreSQL only (SPEC §3.5). Full-text search, row locks for
# gap-free numbering, and the job queue all depend on it, and a second backend
# for development is a second set of behaviors to test.
# --------------------------------------------------------------------------

DATABASE_URL = env("DATABASE_URL", "postgresql://trammel:trammel-dev@127.0.0.1:5433/trammel")
_db = urlparse(DATABASE_URL)
if _db.scheme not in {"postgres", "postgresql"}:
    raise ImproperlyConfigured("DATABASE_URL must be a postgresql:// URL.")
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _db.path.lstrip("/"),
        "USER": unquote(_db.username or ""),
        "PASSWORD": unquote(_db.password or ""),
        "HOST": _db.hostname or "",
        "PORT": str(_db.port or ""),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

# --------------------------------------------------------------------------
# Authentication and session security (SPEC §9)
# --------------------------------------------------------------------------

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

#: Failed sign-ins allowed per account, and per source address, inside the
#: window before further attempts are refused (SPEC §9).
LOGIN_MAX_FAILURES = env_int("LOGIN_MAX_FAILURES", 5)
LOGIN_MAX_FAILURES_PER_IP = env_int("LOGIN_MAX_FAILURES_PER_IP", 20)
# Failures for one account from anywhere before it is locked: a backstop for
# guessing spread over many addresses, high enough that nobody can lock a
# colleague out from one machine (LOGIN_MAX_FAILURES counts per address and IP).
LOGIN_MAX_FAILURES_PER_ACCOUNT = env_int("LOGIN_MAX_FAILURES_PER_ACCOUNT", 50)
LOGIN_FAILURE_WINDOW_MINUTES = env_int("LOGIN_FAILURE_WINDOW_MINUTES", 15)

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = env_int("SESSION_HOURS", 12) * 3600
SESSION_SAVE_EVERY_REQUEST = True
# Idle for SESSION_HOURS signs a person out; this signs them out this many days
# after they signed in, however busy, so a stolen session can't last forever.
SESSION_ABSOLUTE_DAYS = env_int("SESSION_ABSOLUTE_DAYS", 7)
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True

_secure = BASE_URL.startswith("https://")
SESSION_COOKIE_SECURE = _secure
CSRF_COOKIE_SECURE = _secure
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if _secure:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 31536000
    # Plain HTTP that reaches the app (its published port, in external-proxy
    # mode) is sent to HTTPS. A proxy must pass X-Forwarded-Proto; set
    # SECURE_SSL_REDIRECT=false if yours can't. The health check is answered first.
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    # To BASE_URL's own host and port, not whatever plain-HTTP port was used.
    SECURE_SSL_HOST = urlparse(BASE_URL).netloc

# Strict CSP: no inline script, no third-party origins. Scripts that must be
# inline carry the per-request nonce.
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.NONCE],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:", "blob:"],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
    "form-action": [CSP.SELF],
}

# --------------------------------------------------------------------------
# Localization. The business timezone and units are chosen on the setup page
# and stored in the database; TIME_ZONE is only the storage/fallback zone.
# --------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------
# Static and media
# --------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
# The image collects static files with hashed, cache-forever names; everywhere
# else they are served straight from static/ so no build step is needed.
STATIC_HASHED = env_bool("STATIC_HASHED", False)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if STATIC_HASHED
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}
WHITENOISE_USE_FINDERS = not STATIC_HASHED
# WhiteNoise warns when STATIC_ROOT is missing even while the finders serve
# everything; create it rather than teach people to ignore a warning.
STATIC_ROOT.mkdir(parents=True, exist_ok=True)
WHITENOISE_MIMETYPES = {".webmanifest": "application/manifest+json"}
MEDIA_ROOT = Path(env("MEDIA_ROOT", str(BASE_DIR / "data" / "media")))
MAX_UPLOAD_MB = env_int("MAX_UPLOAD_MB", 100)
# Backups and exports (FR-BAK). Keep this on the Linux filesystem in WSL2 and
# copy backups off the machine (SPEC §4.2).
BACKUP_DIR = Path(env("BACKUP_DIR", str(BASE_DIR / "data" / "backups")))
# PostgreSQL client tools; must be at least the server's major version.
PG_DUMP = env("PG_DUMP", "pg_dump")
PG_RESTORE = env("PG_RESTORE", "pg_restore")
# Remove GPS location from photo metadata on upload (FR-DOC-6).
STRIP_GPS_EXIF = env_bool("STRIP_GPS_EXIF", True)
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# --------------------------------------------------------------------------
# Email. SMTP is configured in Settings (FR-ADM-2) and stored encrypted; this
# backend is only the fallback when none is configured.
# --------------------------------------------------------------------------

MAILERS = {"default": {"BACKEND": "django.core.mail.backends.dummy.EmailBackend"}}
# Trammel never sends through this fallback (notifications/services.py builds the
# SMTP connection from Settings), so the deploy check against it does not apply.
SILENCED_SYSTEM_CHECKS = ["mail.E001"]
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "trammel@localhost")

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

LOG_LEVEL = env("LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "{levelname} {asctime} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "handlers": ["console"], "propagate": False},
    },
}

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"
