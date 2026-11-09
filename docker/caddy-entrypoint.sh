#!/bin/sh
# Select the TLS strategy before Caddy starts, and refuse an unknown one.
#
# This used to be a bind mount whose source path contained ${TLS_MODE}, on the
# theory that a wrong value would fail because the file did not exist. It does
# not: the Docker engine creates a missing bind source as a *directory*, so a
# typo left a bogus tls-typo.conf/ folder in the repository and a proxy
# crash-looping on "is a directory" — a real failure, with a message pointing
# nowhere near the cause.
#
# Checking it here costs four lines and says what is wrong.
set -eu

MODES_DIR=/etc/caddy/modes
TLS_MODE="${TLS_MODE:-internal}"
SOURCE="${MODES_DIR}/tls-${TLS_MODE}.conf"

if [ ! -f "$SOURCE" ]; then
    echo "TLS_MODE=${TLS_MODE} is not a supported strategy." >&2
    echo "Choose one of:" >&2
    for candidate in "${MODES_DIR}"/tls-*.conf; do
        [ -f "$candidate" ] || continue
        name="${candidate##*/tls-}"
        echo "  ${name%.conf}" >&2
    done
    echo "Set TLS_MODE in .env and start again." >&2
    exit 1
fi

# Copied rather than symlinked so Caddy reads it even though the source is a
# read-only mount, and so the running config cannot change under a reload.
cp "$SOURCE" /etc/caddy/tls.conf
echo "TLS strategy: ${TLS_MODE}"

# The ports Caddy listens on, which compose also publishes unchanged on the
# host (the Caddyfile's global options read them). They default to 8726 and
# 8727 so the stack can share a machine with anything already on 80 and 443.
#
# The http:// redirect sends the browser to https://{host}, and {host} carries
# no port, so anything but 443 has to be added back or the redirect lands on
# 443, where Trammel is not listening. Hence the suffix.
HTTP_PORT="${HTTP_PORT:-8726}"
HTTPS_PORT="${HTTPS_PORT:-8727}"
for name in HTTP_PORT HTTPS_PORT; do
    eval "value=\${$name}"
    case "$value" in
        ''|*[!0-9]*)
            echo "${name}=${value} is not a port number." >&2
            exit 1
            ;;
    esac
done
if [ "$HTTP_PORT" = "$HTTPS_PORT" ]; then
    echo "HTTP_PORT and HTTPS_PORT are both ${HTTPS_PORT}; they must differ." >&2
    exit 1
fi
export HTTP_PORT HTTPS_PORT

if [ "$HTTPS_PORT" = "443" ]; then
    HTTPS_PORT_SUFFIX=""
else
    HTTPS_PORT_SUFFIX=":${HTTPS_PORT}"
    echo "Publishing HTTPS on port ${HTTPS_PORT}."
fi
export HTTPS_PORT_SUFFIX

# BASE_URL is what the application prints into QR labels and notification
# links, and nothing downstream can tell that it is wrong: a traveler or label with
# the wrong port is discovered by somebody scanning it months later on the shop
# floor. Cheap to check here, so check here.
#
# A warning and not a refusal — an instance behind somebody else's reverse
# proxy can legitimately advertise a port this container never sees.
if [ -n "${BASE_URL:-}" ]; then
    hostport="${BASE_URL#*://}"
    hostport="${hostport%%/*}"
    case "$BASE_URL" in
        http://*) base_port=80 ;;
        *)        base_port=443 ;;
    esac
    case "$hostport" in
        *:*) base_port="${hostport##*:}" ;;
    esac
    if [ "$base_port" != "$HTTPS_PORT" ]; then
        echo "warning: BASE_URL says port ${base_port}, but HTTPS is published on ${HTTPS_PORT}." >&2
        echo "         QR labels and notification links are built from BASE_URL," >&2
        echo "         so they will point somewhere nothing is listening." >&2
    fi
fi

if [ "$TLS_MODE" = "acme-dns" ]; then
    # Empty values here produce a certificate error minutes later, from inside
    # ACME, that reads like a provider problem. Name them now instead.
    for required in ACME_EMAIL ACME_DNS_PROVIDER ACME_DNS_TOKEN; do
        eval "value=\${$required:-}"
        if [ -z "$value" ]; then
            echo "TLS_MODE=acme-dns needs ${required} to be set." >&2
            exit 1
        fi
    done
    case "${SITE_ADDRESS:-}" in
        *.home.arpa|*.local|*.internal|*.lan|localhost)
            echo "SITE_ADDRESS=${SITE_ADDRESS} is a special-use name." >&2
            echo "No public CA can issue for it. Use a real domain, or TLS_MODE=internal." >&2
            exit 1
            ;;
    esac
fi

exec caddy "$@"
