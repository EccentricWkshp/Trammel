#!/usr/bin/env bash
# Publish the GitHub Release for a version tag, which is what running instances'
# update check reads (SPEC §4.3). Safe to re-run: an existing release is left as
# it is.
#
#   GH_TOKEN=... .github/scripts/publish_release.sh v0.1.1
set -euo pipefail

tag=$1
version=${tag#v}

if gh release view "$tag" >/dev/null 2>&1; then
    echo "Release ${tag} already exists."
    exit 0
fi

previous=$(git describe --tags --abbrev=0 --match 'v*' "${tag}^" 2>/dev/null || true)
range=${previous:+${previous}..}${tag}

notes=$(mktemp)
{
    echo "## Docker images"
    echo
    echo "- \`eccentricwkshp/trammel:${version}\`"
    echo "- \`eccentricwkshp/trammel-caddy:${version}\`"
    echo
    echo "Both for linux/amd64 and linux/arm64. To update a Docker install:"
    echo
    echo '```bash'
    echo 'docker compose pull && docker compose up -d'
    echo '```'
    echo
    echo "When an update brings database migrations, the app backs the database up"
    echo "before applying them, and does not start if that backup fails."
    echo
    echo "## Changes${previous:+ since ${previous}}"
    echo
    git log --no-merges --reverse --pretty='- %s' "$range"
} > "$notes"

gh release create "$tag" --verify-tag --title "Trammel ${version}" --notes-file "$notes"
