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
| `BASE_URL` | `https://` + that name + `:` + `HTTPS_PORT`, e.g. `https://trammel.home.arpa:8727`. Links in emails and the QR codes on travelers are built from it. |
| `HTTP_PORT`, `HTTPS_PORT` | The ports Trammel listens on, `8726` and `8727` by default so it can share a machine with anything on 80 and 443. Set `HTTPS_PORT=443` (and drop the port from `BASE_URL`) if the machine is Trammel's alone. |
| `ALLOWED_HOSTS` | The same host name. |
| `TLS_MODE` | `internal`, `acme-dns` or `custom` — see step 4. Already running a reverse proxy for other services? See [Using the reverse proxy you already have](#using-the-reverse-proxy-you-already-have) instead. |

Everything about the business itself — name, currency, time zone, rates — is
entered on the setup page, not here.

## 3. Start it

```bash
docker compose up -d
docker compose logs -f app      # wait for "Listening at: http://0.0.0.0:8080"
```

Open `https://<SITE_ADDRESS>:8727` (or whatever `HTTPS_PORT` is). The port is
part of the address: DNS points a name at a machine, never at a port, so
without it the browser goes to 443 and whatever answers there. A new instance shows the **setup page**, which
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

### Using the reverse proxy you already have

If your network already runs a reverse proxy (Caddy, nginx, Traefik and so on)
in front of other services, let it serve Trammel too, and skip the built-in
proxy. Then Trammel is reached at a plain `https://<name>` with no port, the same as
everything else. In `.env`:

```ini
COMPOSE_FILE=docker-compose.yml:docker-compose.external-proxy.yml
TRUSTED_PROXIES=192.168.1.10          # your proxy's IP address
BASE_URL=https://trammel.example.com  # the name your proxy serves, no port
ALLOWED_HOSTS=trammel.example.com
```

`SITE_ADDRESS`, `TLS_MODE`, `HTTPS_PORT` and the ACME settings are not used. The
app is published on `HTTP_PORT` (8726). Point your proxy at it over plain HTTP.
For Caddy:

```caddyfile
trammel.example.com {
    reverse_proxy <trammel host>:8726 {
        request_buffers 8MB
    }
}
```

Your proxy must:

- **terminate HTTPS** with a certificate devices trust. Trammel only works over
  HTTPS.
- **pass the `Host` header through unchanged.** Caddy does by default.
- **send `X-Forwarded-For` and `X-Forwarded-Proto`.** Caddy does by default.
- **accept uploads up to 512 MB.**
- **buffer request bodies** before passing them on, so someone sending a request
  slowly can't hold one of Trammel's few web threads. nginx does by default;
  Caddy needs `request_buffers`, as above. Limit how long a client may take to send
  its headers (10 seconds is plenty).

Trammel believes `X-Forwarded-For` only from the addresses in `TRUSTED_PROXIES`,
so a device on the network that connects to port 8726 directly cannot forge its
address in the audit log. Firewall that port so only the proxy can reach it, if
you can.

Switching an existing install over: `docker compose rm -sf proxy`, then
`docker compose up -d`.

## 5. Day to day

- **Backups** run daily into the `backup-data` volume. Keep copies off this
  machine: in Settings → Backups and export → Backup settings, switch on any of
  - a **network share** (a Windows share or NAS, over SMB; Trammel connects to
    it itself, so nothing needs mounting),
  - **S3-compatible storage** (AWS S3, Backblaze B2, Cloudflare R2, Wasabi,
    MinIO), or
  - a **folder** the app container can write to, such as a share you mount on
    the host and into the container yourself.

  Each has a test button that writes, lists and deletes a small file. The same
  retention count applies everywhere a copy is kept. Take one now
  with `docker compose exec app python manage.py backup`; restore with
  `docker compose exec app python manage.py restore /data/backups/<file>`.
- **Updates**: the admin is told when a release is out. With Docker, update by
  pulling: `docker compose pull && docker compose up -d`. Migrations run on start;
  when an update brings any, the database is backed up first, and the app does not
  start if that backup fails (`docker compose logs app` says why).
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
- a Windows port forward to the WSL address for each port
  (`netsh interface portproxy add v4tov4 listenport=8727 connectport=8727 connectaddress=<wsl ip>`,
  and the same for 8726), which must be redone when the WSL address changes.

Then allow the ports through Windows Firewall (PowerShell as administrator;
use your own values if you changed `HTTP_PORT` or `HTTPS_PORT`):

```powershell
New-NetFirewallRule -DisplayName "Trammel" -Direction Inbound -Protocol TCP -LocalPort 8726,8727 -Action Allow
New-NetFirewallRule -DisplayName "Trammel HTTP/3" -Direction Inbound -Protocol UDP -LocalPort 8727 -Action Allow
```

With mirrored networking also allow them through the Hyper-V firewall:
`New-NetFirewallHyperVRule -Name Trammel -DisplayName "Trammel" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 8726,8727`.

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

## The customer portal on Cloudflare

The customer portal can be served from Cloudflare instead of from this server,
so Trammel never has to be reachable from the internet: Trammel publishes what
each customer may see to a Cloudflare Worker and, every 30 seconds, pulls back
what they did there. Nothing connects in to your network. Only what customers
can see is copied to Cloudflare, and stopping it removes everything.

Trammel deploys and updates the Worker itself, over Cloudflare's API. A few
things are done once, by hand, in Cloudflare's dashboard:

1. **An account.** The free plan is enough for a small shop.
2. **R2.** Open R2 (Cloudflare's file storage) once and turn it on. Cloudflare
   asks for a payment method first, even for the free allowance: 10 GB stored,
   1 million Class A operations (writes) and 10 million Class B operations
   (reads) a month. The portal stores quote and invoice PDFs, shared files and
   the files customers send, and clears what's no longer shown, so a small
   shop stays well inside it.
3. **Your own address (optional).** To serve the portal at, say,
   `portal.example.com`, the domain's DNS must be on Cloudflare. Without one,
   the portal gets a free `workers.dev` address (open Workers & Pages once to
   choose its name).
4. **An API token.** Cloudflare recommends an **account API token** (Manage
   Account, Account API Tokens, Create Token): it belongs to the account, not
   to you, so it keeps working if your login changes. A user token (My
   Profile, API Tokens, Create Token, Custom token) works too. Give it:
   - Account · Workers Scripts · Edit
   - Account · D1 · Edit
   - Account · Workers R2 Storage · Edit
   - Account · Account Settings · Read
   - Account · Turnstile · Edit (user tokens only; see below)
   - Zone · Workers Routes · Edit, and Zone · Zone · Read (only for your own
     address; limit them to that domain)

   With an account API token, note the **account ID** too (on the account's
   home page in the dashboard, and on Workers & Pages).
5. **A bot check on signing in (optional, for an account API token).** An
   account API token can't manage Turnstile, Cloudflare's bot check, so
   Trammel can't set it up with one. To have it, open Turnstile, Add widget:
   any name, the portal's address as its hostname (your own address, or
   `trammel-portal.<your subdomain>.workers.dev`), mode Managed. Keep its site
   key and secret key for Trammel. A user token with the Turnstile permission
   sets this up by itself. Without either, the portal works without the
   check; signing in is rate-limited anyway.

Then in Trammel:

1. **Email must be encrypted** (Settings, Email): SSL/TLS on port 465 or
   STARTTLS on port 587. The portal on Cloudflare sends sign-in codes itself
   with these settings, and Cloudflare doesn't allow port 25.
2. Turn on the **Customer portal** and **Edge portal (Cloudflare)** modules
   (Settings, Modules).
3. **Settings, Portal on Cloudflare**: paste the token, enter the account ID
   (needed for an account API token), give your own address if you have one,
   paste the Turnstile keys if you made a widget, save, then **Deploy to
   Cloudflare**. The page follows the
   deploy step by step and shows the portal's address when it's done.

   A new custom address waits until it's in DNS before Trammel looks it up.
   If it still doesn't answer from Trammel's server (a DNS server, such as a
   Pi-hole or your router, may remember "no such name" for up to 30 minutes),
   the deploy is kept and the page says it's waiting: Trammel checks every 30
   seconds and puts the portal live by itself when it answers.

While the portal is on Cloudflare, Trammel's own portal pages are off (staff
can still view the portal as a customer sees it), and links in emails point at
Cloudflare. Customers' access, the portal switch and `PORTAL_OFF` work as
before: turning the portal off closes it on Cloudflare and signs everyone out
within 30 seconds. **Stop using it** on the same page removes everything
published and brings Trammel's own pages back; **Remove it from Cloudflare**
then deletes the Worker, its database, its storage and the bot check it set
up (one you made by hand stays). Updating
Trammel updates the Worker to match, within the hour.

A closed, declined or canceled job comes off Cloudflare 90 days after it last
changed (Settings, Portal on Cloudflare sets how long; 0 takes it off at
once). If Cloudflare can't be reached, customers' actions wait there and
Trammel catches up when it can; after ten minutes admins see a banner. If the
portal's database is ever lost or made again, Trammel publishes everything
again by itself.

### Online stores through the portal on Cloudflare

While the portal is on Cloudflare, it also stands in for Trammel with the
online stores, at the same paths, so nothing else has to be public:

- **Shopify webhooks** go to the portal's address
  (`https://<portal address>/imports/shopify/webhook/`, shown on Settings,
  Online stores). The portal keeps each one for Trammel, which checks
  Shopify's signature before taking the order.
- **Etsy's and eBay's sign-in** come back to the portal's address, which sends
  your browser on to Trammel (your browser can reach it; the internet can't).
  Settings, Online stores shows the addresses to give Etsy and eBay.
- **eBay's account deletion notices**: Settings, Portal on Cloudflare shows the
  endpoint and verification token to give eBay (Alerts & Notifications in its
  developer portal). The portal answers eBay's check; Trammel checks each
  notice's signature with eBay's published key before wiping anything. No
  separate relay is needed.

## Online payments (Stripe)

Customers can pay invoices online, by card or bank through Stripe. Trammel
makes a Stripe payment link for each open invoice's balance (shown in the
customer portal, on Cloudflare or not, and in invoice emails) and asks Stripe
every minute what was paid, recording each payment against its invoice. Stripe
never has to reach Trammel: there's no webhook to set up.

1. **A Stripe account**, and in its dashboard (Developers, API keys) a
   **restricted key** with:
   - Payment Links · Write
   - Checkout Sessions · Read
   - Products · Write, and Prices · Write (each link carries its invoice's
     amount)

   Make it in **test mode** first: a test key (`rk_test_…`) takes no real
   money, and card 4242 4242 4242 4242 (any future date, any CVC) pays.
2. Turn on the **Online payments** module (Settings, Modules).
3. **Settings, Online payments**: paste the key, switch payments on, save, and
   **Test the key**. Within a minute each open invoice has a link; an
   invoice's page shows it, to copy into a message if you like.
4. Pay a test invoice from the portal. You come back to the invoice, and
   within a minute the payment is recorded (method Stripe, with Stripe's
   reference), the customer gets the usual receipt, and staff get a "Paid
   online" email. Then swap in a live key.

A link is for the whole balance and works once. When the balance changes (a
payment by check, a credit note), the invoice is voided, or it's paid, the old
link stops working and a new one is made if anything is still owed. A bank
payment that takes days shows as being processed, with no link meanwhile, so
it can't be paid twice. Stripe's fees come out of what you receive; customers
pay the invoice amount. A refund is made in Stripe's dashboard, then recorded
in Trammel as a payment of a negative amount.

## Shopify order webhooks

Fetching orders by date needs nothing from the internet. Webhooks, which bring
each order in within seconds, need Shopify to reach Trammel over HTTPS. If you
switch them on, make only the webhook address public, never the rest of
Trammel: the public proxy or tunnel forwards `/imports/shopify/webhook/` and
answers everything else with "not found". For nginx:

```nginx
location = /imports/shopify/webhook/ {
    proxy_pass https://trammel.home.arpa:8727;   # Trammel's own address
    proxy_set_header Host trammel.home.arpa;     # a name in ALLOWED_HOSTS
    proxy_set_header X-Forwarded-Proto https;
}
location / { return 404; }
```

The address answers "not found" until the Online orders module and its
webhooks are switched on, and only accepts requests Shopify has signed. Fetch
by date now and then anyway: Shopify gives up on a webhook it can't deliver.

## eBay account deletion notices

With the customer portal on Cloudflare, it takes these for you (see above).
Otherwise:

eBay won't switch an app's keys on for real orders until the app deals with
its marketplace account deletion notices, and those need a public HTTPS address
that answers eBay's challenge and stays up. Trammel doesn't host that. Run a
small relay on the internet instead: it answers eBay, keeps each notice, and
forwards it to Trammel:

```
POST /api/v1/imports/ebay/deletion-notices
Authorization: Bearer <API key>
Content-Type: application/json

<eBay's notice body, as eBay sent it>
```

Give the relay its own API key holding only "Forward eBay account deletion
notices" (your profile, API keys). The relay should check eBay's signature before
forwarding. A notice sent twice is handled once, so the relay can retry freely.
Each notice wipes the buyer's details from the stored eBay orders and waits on
Online orders, Deletion requests, for you to anonymize the customer or dismiss
it. Without a relay, enter notices by hand on that page.

