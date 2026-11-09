# Trammel

Workshop management for small shops — customers, quoting, jobs and work
orders, time and material tracking, invoicing and closeout for a 0–5 person
job shop: woodworking, metal fabrication, 3D printing, laser work.
Self-hosted: the data stays on the shop's own hardware.

## Images

| Image | What it is |
| --- | --- |
| `eccentricwkshp/trammel` | The app. The same image runs the web server (`web`, the default) and the background worker (`worker`). |
| `eccentricwkshp/trammel-caddy` | The built-in HTTPS proxy: Caddy with its own certificate authority, Let's Encrypt over a DNS challenge, or your own certificate. Optional: a network that already runs a reverse proxy can use that instead. |

Both are built for `linux/amd64` and `linux/arm64`. Tags: the release version
(e.g. `0.1.0`) and `latest`.

## Running it

Trammel runs as four containers — Caddy, the app, the worker and PostgreSQL —
started together with Docker Compose. Use the `docker-compose.yml` and
`.env.example` from the Trammel project; its `docs/INSTALL.md` walks through
the settings, HTTPS certificates and backups.

```bash
cp .env.example .env      # set SECRET_KEY, POSTGRES_PASSWORD, SITE_ADDRESS, BASE_URL
docker compose up -d
```

Then open `https://<SITE_ADDRESS>:8727`. To put Trammel behind a reverse
proxy you already run, so it is reached at a plain `https://<name>`, see the
install guide's "Using the reverse proxy you already have". A new instance shows a setup page that
creates the first admin account; there is no default login.

## Updating

```bash
docker compose pull && docker compose up -d
```

Database migrations run when the app starts, and a backup is taken first.
