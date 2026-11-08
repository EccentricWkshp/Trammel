# Installing Trammel

Trammel runs as four containers — Caddy (HTTPS), the app, a background worker,
and PostgreSQL — on any amd64 or arm64 machine with Docker. The reference host
is Debian or Ubuntu on WSL2 under Windows 11; the WSL2 section below covers what
is different there.

## 1. Get the files

```bash
git clone https://github.com/EccentricWkshp/trammel.git
cd trammel
cp .env.example .env
```

## 2. Edit `.env`

| Setting | What to put |
| --- | --- |
| `SECRET_KEY` | A long random value: `python3 -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `POSTGRES_PASSWORD` | Another long random value. |
| `SITE_ADDRESS` | The name devices will use, e.g. `trammel.home.arpa` or `shop.example.com`. |
| `BASE_URL` | `https://` + that name (plus `:port` if HTTPS is not on 443). Links in emails and the QR codes on travelers are built from it. |
| `ALLOWED_HOSTS` | The same host name. |
| `TLS_MODE` | `internal`, `acme-dns` or `custom` — see step 4. |

Everything about the business itself — name, currency, time zone, rates — is
entered on the setup page, not here.

## 3. Start it

```bash
docker compose up -d
docker compose logs -f app      # wait for "Listening at: http://0.0.0.0:8080"
```

Open `https://<SITE_ADDRESS>`. A new instance shows the **setup page**, which
creates the system admin account; there is no default login. If the machine
may be reachable by anyone else before you finish setup, set `SETUP_TOKEN` in
`.env` first and the page will ask for it.

## 4. HTTPS certificates

HTTPS is required: the shop-floor app and camera QR scanning only work over it.

- **`internal`** (default): Caddy runs its own certificate authority. Every
  phone, tablet and computer that uses Trammel must trust its root certificate
  once. Export it with
  `docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt ./trammel-root.crt`
  and install it on each device (on iOS, also enable it under
  Settings → General → About → Certificate Trust Settings).
- **`acme-dns`**: a real Let's Encrypt certificate using a DNS challenge. Nothing
  to install on devices and the instance never needs to be reachable from the
  internet — but you need a domain whose DNS you control. Set `ACME_EMAIL`,
  `ACME_DNS_PROVIDER` (`acmedns`, `cloudflare`, `desec` or `duckdns` are built
  in) and `ACME_DNS_TOKEN`.
- **`custom`**: put `fullchain.pem` and `privkey.pem` in `config/caddy/certs/`.

## 5. Day to day

- **Backups** run daily into the `backup-data` volume. Keep copies off this
  machine: in Settings → Backups and export, set an off-box folder (a mounted
  network share), S3-compatible storage (AWS S3, Backblaze B2, Cloudflare R2,
  Wasabi, MinIO — use **Test S3 connection** to check it), or both. The same
  retention count applies everywhere a copy is kept. Take one now
  with `docker compose exec app python manage.py backup`; restore with
  `docker compose exec app python manage.py restore /data/backups/<file>`.
- **Updates**: the admin is told when a release is out. With Docker, update by
  pulling: `docker compose pull && docker compose up -d`. Migrations run on start.
  The container never updates itself (that would need the Docker socket).
- **Full export**: Settings → Backups and export → Build export gives every
  record as newline-delimited JSON plus every file, readable without Trammel.
- **Email**: configure your SMTP server in Settings → Email. Nothing is sent
  until you do, and nothing at all while "outbound traffic" is switched off.

## WSL2 (Windows 11) specifics

### Reaching it from the LAN

WSL2 sits behind NAT by default, so other devices cannot reach it. Either:

- **Mirrored networking** (recommended): in `%UserProfile%\.wslconfig`
  ```ini
  [wsl2]
  networkingMode=mirrored
  hostAddressLoopback=true
  ```
  then `wsl --shutdown` and start the distribution again; or
- a Windows port forward from 443 to the WSL address
  (`netsh interface portproxy add v4tov4 listenport=443 connectport=443 connectaddress=<wsl ip>`),
  which must be redone when the WSL address changes.

Then allow HTTPS through Windows Firewall (PowerShell as administrator):

```powershell
New-NetFirewallRule -DisplayName "Trammel HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
```

With mirrored networking also allow it through the Hyper-V firewall:
`New-NetFirewallHyperVRule -Name TrammelHTTPS -DisplayName "Trammel HTTPS" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 443`.

### Starting on boot and staying up

WSL does not start until something launches it, and shuts an idle distribution
down.

1. Enable systemd in the distribution — `/etc/wsl.conf`:
   ```ini
   [boot]
   systemd=true
   ```
   and enable Docker: `sudo systemctl enable --now docker`. The containers use
   `restart: unless-stopped`, so they come back with Docker.
2. Keep WSL from idling out — in `.wslconfig` under `[wsl2]`: `vmIdleTimeout=-1`.
3. Start it at boot with a Windows scheduled task (run whether or not a user is
   signed in): program `wsl.exe`, arguments `-d <Distro> --exec /bin/true`,
   trigger "At startup".

### Storage

Keep the project and Docker's volumes on the Linux filesystem (for example
`~/trammel`), **never under `/mnt/c`** — that is much slower and unsafe for
PostgreSQL. Point the off-box backup folder at a Windows path, NAS or other disk
*outside* the WSL virtual disk (e.g. `/mnt/d/TrammelBackups`, mounted into the
app container), so losing the distribution does not also lose the backups.

### Clock

WSL2's clock can drift after the host sleeps, which breaks two-factor codes and
certificate checks. With systemd enabled, turn on time sync inside the
distribution: `sudo timedatectl set-ntp true` (install `systemd-timesyncd` if it
is missing). `wsl --shutdown` also resets the clock.
