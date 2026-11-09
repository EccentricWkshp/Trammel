#!/usr/bin/env bash
# Migrations are tested from the previous release (SPEC §11): build a database
# with the last release's code, fill it with demo data, then migrate it with
# this checkout. Data migrations meet real rows rather than an empty schema.
#
#   DATABASE_URL=postgresql://user:pass@host:5432/anydb .github/scripts/upgrade_from_previous.sh
#
# DATABASE_URL names the server and credentials; a scratch database called
# trammel_upgrade is created there and dropped afterwards. Needs the full git
# history with tags (actions/checkout with fetch-depth: 0).
set -euo pipefail

python=${PYTHON:-python}
here=$(pwd)

# The newest release tag before this commit. On the release commit itself that
# is the release before it, which is the upgrade that matters.
previous=$(git describe --tags --abbrev=0 --match 'v*' HEAD^ 2>/dev/null || true)
if [ -z "$previous" ]; then
    echo "No earlier release to upgrade from."
    exit 0
fi
echo "Upgrading from ${previous} to $(git rev-parse --short HEAD)"

base_url=$DATABASE_URL

# Run SQL statements on the server's maintenance database.
server_sql() {
    BASE_URL="$base_url" "$python" -c '
import os, sys, psycopg
from urllib.parse import urlparse
url = urlparse(os.environ["BASE_URL"])._replace(path="/postgres").geturl()
with psycopg.connect(url, autocommit=True) as conn:
    for statement in sys.argv[1:]:
        conn.execute(statement)
' "$@"
}

work=$(mktemp -d)
cleanup() {
    git -C "$here" worktree remove --force "$work/src" 2>/dev/null || true
    server_sql "DROP DATABASE IF EXISTS trammel_upgrade WITH (FORCE)" || true
    rm -rf "$work"
}
trap cleanup EXIT

server_sql "DROP DATABASE IF EXISTS trammel_upgrade WITH (FORCE)" "CREATE DATABASE trammel_upgrade"
export DATABASE_URL="${base_url%/*}/trammel_upgrade"
export DEBUG=true

git worktree add --detach "$work/src" "$previous" >/dev/null
"$python" -m venv "$work/venv"
if [ -x "$work/venv/bin/python" ]; then old="$work/venv/bin/python"; else old="$work/venv/Scripts/python.exe"; fi
"$old" -m pip install --quiet -r "$work/src/requirements.txt"

echo "--- ${previous}: migrate and load demo data"
(
    cd "$work/src"
    "$old" manage.py migrate --noinput -v0
    "$old" manage.py shell -c "from trammel.accounts.models import User; User.objects.create_admin('upgrade@example.com', 'upgrade-test-password-1', full_name='Upgrade Test')"
    "$old" manage.py seed_demo --yes
)

echo "--- this checkout: migrate the ${previous} database"
"$python" manage.py migrate --noinput
"$python" manage.py check --fail-level WARNING
echo "Upgrade from ${previous} succeeded."
