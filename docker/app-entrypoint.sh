#!/bin/sh
# Trammel container entrypoint: "web" (default), "worker", or any manage.py command.
set -eu

case "${1:-web}" in
    web)
        # Migrations run on every start (SPEC §11); every installed module's
        # migrations run whether or not it is enabled, so the schema never diverges.
        # When an update brings migrations, the database is backed up first (FR-BAK-4).
        python manage.py migrate_safely
        python manage.py createcachetable
        exec gunicorn config.wsgi:application \
            --bind 0.0.0.0:8080 --workers "${WEB_WORKERS:-3}" --timeout 120 \
            --access-logfile - --error-logfile -
        ;;
    worker)
        exec python manage.py run_worker
        ;;
    *)
        exec python manage.py "$@"
        ;;
esac
