# Development

## Run it locally

Requirements: Python 3.14 and Docker (for PostgreSQL). On the reference
Windows machine, Docker runs inside WSL2 with mirrored networking, so the
database container is reachable from Windows on `127.0.0.1`.

```bash
# 1. Database — PostgreSQL 18 on 127.0.0.1:5433 (run from WSL if Docker lives there)
docker compose -f docker-compose.dev.yml up -d

# 2. Python environment
py -3.14 -m venv venv
venv/Scripts/python -m pip install -r requirements-dev.txt     # POSIX: venv/bin/python

# 3. Configuration — a .env with DEBUG=true is enough for development
cp .env.example .env    # then set DEBUG=true

# 4. Schema and server
venv/Scripts/python manage.py migrate
venv/Scripts/python manage.py runserver
```

Open <http://localhost:8000>. A fresh database sends every page to the setup
page, which creates the system admin — there is no default login.

Background jobs (email, imports, thumbnails, backups, overdue reminders) run in a separate
process: `venv/Scripts/python manage.py run_worker`. Without it they queue up
and nothing is lost.

**Demo data.** `manage.py seed_demo --yes` fills a fresh instance (after the
setup page) with customers, quotes, jobs at every stage, work orders, time,
invoices and payments, all made through the real services. It refuses to run
twice.

**PDFs.** Quote, invoice and traveler PDFs need WeasyPrint's system libraries
(Pango). The Docker image has them; a Windows virtualenv usually does not, and
the PDF buttons then say so. Print → Save as PDF works everywhere.

**PostgreSQL only.** There is no SQLite fallback. Full-text search, row locks
for gap-free numbering and the job queue all depend on PostgreSQL, and
HomeAutoShop's experience (a search that passed on SQLite and failed in
production) is the reason not to keep a second backend for convenience.

## Tests and checks

```bash
venv/Scripts/python -m pytest            # uses config.settings_test, against PostgreSQL
venv/Scripts/ruff check . && venv/Scripts/ruff format --check .
venv/Scripts/python manage.py makemigrations --check --dry-run --settings=config.settings_test
```

Deprecation warnings fail the suite, so an upgrade that deprecates something
is noticed the day it lands. `tests/testapp` is a module that exists only in
the test suite; it exercises lifecycles, record scoping, number series and
module enable/disable before real modules depend on them.

On Windows, set `PYTHONUTF8=1` in the shell. Python otherwise reads and writes
files as cp1252, which corrupts non-ASCII characters in source files.

CI also upgrades a database from the previous release: it migrates one with the
last release tag's code, loads demo data, then migrates it with this checkout.
Run the same locally with
`DATABASE_URL=… PYTHON=venv/Scripts/python bash .github/scripts/upgrade_from_previous.sh`
(it creates and drops a scratch database called `trammel_upgrade`).

`tests/test_api_flow.py` drives the API with a real key from first quote to a
paid invoice, then calls every GET endpoint in the OpenAPI document, so an
endpoint that only fails at request time is caught.

## Releasing

1. Set `VERSION` (semantic versioning) and commit.
2. Tag `v<VERSION>` on that commit and push the tag. CI refuses a tag that does
   not match `VERSION`.
3. CI tests, then publishes both images for amd64 and arm64 with the Docker Hub
   descriptions from `docs/DOCKERHUB.md`, then the GitHub Release
   (`.github/scripts/publish_release.sh`). Running instances learn of the
   version from that Release, not from the tag or the images.

## How the core fits together

Every module is a Django app whose `AppConfig` subclasses
`trammel.core.modules.ModuleConfig` and carries a `ModuleManifest`. Add it to
`INSTALLED_APPS` by its **config class path** (`trammel.jobs.apps.JobsConfig`):
because every `apps.py` imports `ModuleConfig`, Django's automatic config
detection sees two candidates and would silently fall back to a plain
`AppConfig`, and the module would not register.

| Concern | Where | Rule |
| --- | --- | --- |
| Module registry | `core/modules.py` | Manifests declare permissions, menus, URLs, API router and number series. URLs and API routers are mounted from manifests; nothing is listed by hand. Disabling a module hides it — menus, record-page tabs, search — and makes its non-read permissions fail everywhere (SPEC §4.5); its data stays. `routes_when_disabled=False` makes its URLs not resolve at all (FR-PRT-5). `disable_check` names a function that can refuse disabling (stock while materials are tracked). Menu sections are `main`, `admin` (Settings) and `floor` (the shop-floor app's top bar). A menu entry with `hidden_with="stock"` gives way while that module is on (Materials, which stock lists in full; `shop.hooks` sends the materials list and the add material form to stock's pages while it is on). |
| Permissions | `core/access.py` | Codenames are `<module>.<action>`. `has_perm` checks the action; `scoped` / `get_or_404` add the per-record check. Views and API endpoints load records through `get_or_404` and lists through `scoped`. An unknown codename raises, so typos fail tests. |
| Audit log | `core/audit.py` | Middleware binds the actor and IP; service code calls `audit.record*()`. Append-only in the ORM and by a database trigger. |
| Lifecycles | `core/lifecycle.py` | Declare `Transition`s on a `LifecycleModel`; change state only with `Lifecycle.apply()`. Assigning the status field and saving raises. Reopen and force transitions require a reason and are recorded as such. |
| Numbering | `core/numbering.py` | `next_number(key)` inside the transaction that saves the document. Gap-free because a rollback undoes the increment. |
| Cross-module hooks | `jobs/hooks.py`, `workorders/hooks.py`, `quotes/services.py`, `billing/services.py` | A module never imports one that depends on it. Instead, later modules register: checks that block a job transition (`hooks.check("closed")`), items a forced closeout must resolve, duplication steps, and callbacks such as `quotes.services.on_approved` and `billing.services.on_issued`. `workorders.hooks.material_handler` lets stock take over recording a tracked material; `planned_note` adds what is on hand beside planned materials; `carry_over` plans more for a quote line (a product's bill of materials); `operation_done` and `planned_use` decide what happens to planned materials when an operation is done. Each module's `wiring.py` (imported automatically) is where it registers. |
| Record pages | `core/panels.py`, `templates/partials/tabs.html` | Customer, job, quote, work order, invoice, product and material pages are tabbed with plain `?tab=` links (no script). Other modules add tabs with `panels.register(...)`. |
| Archive and delete | `core/archive.py`, `templates/partials/archive_actions.html`, `templates/core/confirm_delete.html` | One pattern for products, materials, vendors, machines, stock locations and quote and invoice templates. Archiving sets `is_active = False`: the record stays where it is used and pickers leave it out. Deleting (to the trash) is allowed only while `archive.in_use()` finds nothing pointing at the record: any reverse foreign key with rows counts, including `related_name="+"`, except cascading parts (a product's bill of materials), which are checked in turn. A module that keeps a part another way registers `archive.on_delete("app.model")` (stock drops a material's unused stock record). Views call `archive.archive_view` and `archive.delete_view` after their own permission check. |
| Notices and search | `core/notices.py`, `core/search.py` | Site-wide banners (no backup yet, SMTP not set up) and the top search box; modules register providers. |
| Calendar and reports | `core/agenda.py`, `core/report_cards.py` | Modules add dated entries to the calendar (`agenda.register`, e.g. purchase orders arriving) and cards to the Reports page (`report_cards.register`), shown while the module is on and the person holds the permission. |
| Scanning | `core/scan.py` | Labels carry `/s/<id>/`; modules register `scan.record` (open a record by id), `scan.text` (a typed or scanned code: work order or lot number, barcode) and `scan.unknown` (what to offer when nothing matched). The shop floor's find page and the short link both go through it. |
| Home screen | `core/dashboard.py`, `templates/core/dashboard.html` | Modules add sections with `dashboard.register(key, title, order=, permission=)`; a section returns at most `dashboard.LIMIT` rows and a link to the full list. Sections of disabled modules, or that the user lacks the permission for, are left out. Each user's choice and order is stored in `User.home_sections`. |
| Printable documents | `core/web.py` (`document_view`, `document_response`) | Quotes, invoices and travelers are served under their own content security policy: inline styles, `data:` images, no network, sandboxed. `?print=1` adds the one script allowed — a print call carrying the request's CSP nonce. Quote and invoice bodies are user templates rendered in a sandboxed Jinja environment (`core/templating.py`). |
| Dates and times | `core/formats.py`, filters `d dt t md mdt pct` | The business picks the date format (US by default) and the first day of the week (Sunday by default). Times show in the user's time zone, else the business's. Always format with these filters, never a hard-coded pattern. |
| Sales tax | `tax/services.py`, `tax/importers.py` | Rates never come from the network at sale time. `determine()` applies the sourcing rule (pickups use the shop's ZIP); `taxable_shipping()` applies the shipping setting. Issued invoices keep a snapshot (`InvoiceTax`), shown to customers as one combined line. SST files import from an upload or a URL, ZIP or plain. |
| Machine rates | `shop/rates.py`, `static/shop/machine_rate.js` | The calculator's arithmetic. The JS is a live preview of the same formulas; the Python result is what job costing uses (`Machine.cost_rate`). |
| Units | `core/units.py`, `Material.convert` | Exact conversions within a dimension (factors are fractions); crossing dimensions raises, and two count units (box, each) never convert. A material converts further through its sheet size and its own rows (`MaterialUnit`: 100 each = 1 box, 1000 g = 1 spool), always to and from its stock unit. |
| Stock | `stock/services.py` | On hand is the sum of an append-only ledger (`StockMove`; database triggers refuse UPDATE and DELETE). Lots keep running totals, checked by `manage.py stock_check`. Lock the `StockItem` row, then its lots. A draw costs value × taken / on hand to the cent; the draw that empties a lot takes what value is left. Never write an UPDATE data migration on `stock_stockmove`: correct with moves. |
| Purchasing | `purchasing/services.py`, `stock/hooks.py` | Landed cost: `allocate()` spreads charges in whole cents by largest remainder; the delivery completing a line takes what is left. Receipts are append-only; undoing writes a reversing receipt and an `unreceive` stock move. Purchasing tells stock what is on order (`stock.hooks.on_order`) and adds “Create purchase orders” to the restock list (`restock_action`). |
| Money | `core/money.py` | `MoneyField` (2 dp) for document amounts, `RateField` (4 dp) for unit rates, `round_money()` half-up — the one rounding rule. Inputs show no padding zeros (money keeps two places: `MoneyInput`); documents get quantities through `plain_decimal()`, never `normalize()` (which writes 120 as `1.2E+2`). |
| Forms | `core/renderers.py`, `templates/partials/field.html`, `partials/form_body.html` | One field layout everywhere: each field in a `.field` wrapper, checkboxes before their label. `core/widgets.py` has the multi-select dropdown. `static/trammel.js` holds the small enhancements (dropdowns, script tabs); every page works without it. |
| Template help | `core/placeholders.py`, each module's `placeholders.py` | Every user template kind (quote, invoice, agreement, each email) registers its placeholders and a preview context — the newest real record, or sample data. The editor (`core/template_editor.html`) previews unsaved text through `core:template_preview`; `/settings/placeholders/` lists them all. |
| Settings | `core/models.py` | Infrastructure in the environment (`.env.example`); business configuration in singleton rows edited in the UI. |

Writes go through each module's `services.py`, which validates, saves and
audits. Web views and API endpoints both call the services, so the two cannot
drift apart (FR-API-1).

## API

`/api/v1/` with OpenAPI docs at `/api/v1/docs` (signed-in users). Authenticate
with a personal API key (`Authorization: Bearer trm_…`, created on the profile
page) or the browser session (CSRF-checked for writes).

A key holds the permissions chosen when it was made, never more, and never
counts as the system admin. So what only admins do on the web has its own
permission for the API: the audit log (`/audit/`) needs "View the audit log",
and the portal on Cloudflare (`/edge/status`, `/edge/settings`, `/edge/sync`,
`/edge/deploy`, `/edge/stop`, `/edge/remove`) needs "edge.manage", and the
portal's file settings (`PATCH /portal/settings`) need "portal.manage". A key
made before a permission existed doesn't have it; make a new one.

Files come back as their bytes, not JSON: `GET /files/revisions/{id}/download`
for any file revision, and `GET /portal/uploads/{id}/file` for one a customer
sent. Viewing the portal as a customer would is the one staff feature left to
the browser.
