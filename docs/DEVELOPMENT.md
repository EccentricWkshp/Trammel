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

## How the core fits together

Every module is a Django app whose `AppConfig` subclasses
`trammel.core.modules.ModuleConfig` and carries a `ModuleManifest`. Add it to
`INSTALLED_APPS` by its **config class path** (`trammel.jobs.apps.JobsConfig`):
because every `apps.py` imports `ModuleConfig`, Django's automatic config
detection sees two candidates and would silently fall back to a plain
`AppConfig`, and the module would not register.

| Concern | Where | Rule |
| --- | --- | --- |
| Module registry | `core/modules.py` | Manifests declare permissions, menus, URLs, API router and number series. URLs and API routers are mounted from manifests; nothing is listed by hand. Disabling a module hides it — menus, record-page tabs, search — and makes its non-read permissions fail everywhere (SPEC §4.5); its data stays. `routes_when_disabled=False` makes its URLs not resolve at all (FR-PRT-5). |
| Permissions | `core/access.py` | Codenames are `<module>.<action>`. `has_perm` checks the action; `scoped` / `get_or_404` add the per-record check. Views and API endpoints load records through `get_or_404` and lists through `scoped`. An unknown codename raises, so typos fail tests. |
| Audit log | `core/audit.py` | Middleware binds the actor and IP; service code calls `audit.record*()`. Append-only in the ORM and by a database trigger. |
| Lifecycles | `core/lifecycle.py` | Declare `Transition`s on a `LifecycleModel`; change state only with `Lifecycle.apply()`. Assigning the status field and saving raises. Reopen and force transitions require a reason and are recorded as such. |
| Numbering | `core/numbering.py` | `next_number(key)` inside the transaction that saves the document. Gap-free because a rollback undoes the increment. |
| Cross-module hooks | `jobs/hooks.py`, `workorders/hooks.py`, `quotes/services.py`, `billing/services.py` | A module never imports one that depends on it. Instead, later modules register: checks that block a job transition (`hooks.check("closed")`), items a forced closeout must resolve, duplication steps, and callbacks such as `quotes.services.on_approved` and `billing.services.on_issued`. Each module's `wiring.py` (imported automatically) is where it registers. |
| Record pages | `core/panels.py`, `templates/partials/tabs.html` | Customer, job, quote, work order and invoice pages are tabbed with plain `?tab=` links (no script). Other modules add tabs with `panels.register(...)`. |
| Notices and search | `core/notices.py`, `core/search.py` | Site-wide banners (no backup yet, SMTP not set up) and the top search box; modules register providers. |
| Printable documents | `core/web.py` (`document_view`, `document_response`) | Quotes, invoices and travelers are served under their own content security policy: inline styles, `data:` images, no network, sandboxed. `?print=1` adds the one script allowed — a print call carrying the request's CSP nonce. Quote and invoice bodies are user templates rendered in a sandboxed Jinja environment (`core/templating.py`). |
| Dates and times | `core/formats.py`, filters `d dt t md mdt pct` | The business picks the date format (US by default) and the first day of the week (Sunday by default). Times show in the user's time zone, else the business's. Always format with these filters, never a hard-coded pattern. |
| Sales tax | `tax/services.py`, `tax/importers.py` | Rates never come from the network at sale time. `determine()` applies the sourcing rule (pickups use the shop's ZIP); `taxable_shipping()` applies the shipping setting. Issued invoices keep a snapshot (`InvoiceTax`), shown to customers as one combined line. SST files import from an upload or a URL, ZIP or plain. |
| Machine rates | `shop/rates.py`, `static/shop/machine_rate.js` | The calculator's arithmetic. The JS is a live preview of the same formulas; the Python result is what job costing uses (`Machine.cost_rate`). |
| Units | `core/units.py` | Exact conversions within a dimension (factors are fractions); crossing dimensions raises. |
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
