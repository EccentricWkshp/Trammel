# Trammel — one image for the web app and the worker.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings

# pg_dump/pg_restore must be at least the server's major version, so the client
# comes from PGDG to match the postgres image in docker-compose.yml.
ARG PG_MAJOR=18

# Pango and fonts are for WeasyPrint (PDF quotes, invoices, travelers).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client-${PG_MAJOR} \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b fonts-dejavu-core fonts-liberation \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt "gunicorn>=23"

COPY . .
RUN chmod +x /app/docker/*.sh

# Runs unprivileged (SPEC §9).
RUN useradd --system --uid 10001 --create-home trammel \
    && mkdir -p /data/media /data/backups /app/staticfiles \
    && chown -R trammel:trammel /data /app/staticfiles
USER trammel

# Static files are collected at build time with hashed names; WhiteNoise serves them.
RUN DEBUG=true STATIC_HASHED=true python manage.py collectstatic --noinput --clear

ARG VERSION=0.0.0+unknown
ARG VCS_REF=""
ENV APP_REVISION=${VCS_REF}

LABEL org.opencontainers.image.title="Trammel"
LABEL org.opencontainers.image.description="Workshop management for small shops."
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.revision="${VCS_REF}"
LABEL org.opencontainers.image.source="https://github.com/EccentricWkshp/trammel"
LABEL org.opencontainers.image.documentation="https://github.com/EccentricWkshp/trammel/blob/main/docs/INSTALL.md"

EXPOSE 8080
ENTRYPOINT ["/app/docker/app-entrypoint.sh"]
CMD ["web"]
