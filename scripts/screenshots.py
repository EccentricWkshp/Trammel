"""Screenshots of a demo instance for the README and docs (docs/screenshots/).

On a development instance holding only demo data (`manage.py seed_demo`), with
its server on http://127.0.0.1:8000 and the worker's queue run once (for file
previews):

    pip install playwright && python -m playwright install chromium
    set TRAMMEL_SHOTS_EMAIL=... and TRAMMEL_SHOTS_PASSWORD=... (an admin)
    python scripts/screenshots.py [out dir]

TRAMMEL_SHOTS_CHROME, set to a Chrome or Chromium you have, uses that
instead of Playwright's own (no `playwright install` needed). The demo's 3D
models (`seed_demo --only models` on an older demo) give the 3D viewer and
flat pattern pictures.

It signs in as that admin for the staff pages, and into the customer portal
with one-time sign-in codes it makes for two demo customers. Two notices about
the instance itself (no backup yet, two-factor not yet required of admins) are
left out of the pictures. Playwright is a development tool only, not a
requirement of Trammel.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

BASE = os.environ.get("TRAMMEL_SHOTS_URL", "http://127.0.0.1:8000")

#: Notices about the instance itself, not features: left out of the pictures.
HIDE = """() => {
  const own = /^(No backup has been taken|Action item: require two-factor)/;
  for (const p of document.querySelectorAll('p.notice')) {
    if (own.test(p.textContent.trim())) p.remove();
  }
  for (const box of document.querySelectorAll('.messages')) {
    if (!box.querySelector('p')) box.remove();
  }
}"""


def pages():
    """(staff pages, {customer: portal pages}, the customers), from the demo data."""
    from trammel.billing.models import Invoice
    from trammel.customers.models import Customer
    from trammel.jobs.models import Job
    from trammel.quotes.models import QuoteRevision
    from trammel.workorders.models import WorkOrder

    brew = Customer.objects.get(name="Foundry Brewing")
    cafe = Customer.objects.get(name="Lantern Café")
    grace = Customer.objects.get(name="Grace Hollis")
    taps = Job.objects.get(title="Taproom tap handles")
    table = Job.objects.get(title="Walnut dining table")
    menu = QuoteRevision.objects.filter(quote__job__title="Menu boards").order_by("-number")[0]
    coasters = Invoice.objects.get(job__title="Wholesale coaster sets", kind="final")
    deposit = Invoice.objects.get(job=table, kind="deposit")
    wo = WorkOrder.objects.filter(job=table).first()
    staff = [
        ("dashboard", "/"),
        ("jobs", "/jobs/"),
        ("job", taps.get_absolute_url()),
        ("quote", menu.quote.get_absolute_url()),
        ("work-order", wo.get_absolute_url()),
        ("schedule", "/work-orders/schedule/"),
        ("invoice", coasters.get_absolute_url()),
        ("invoice-document", f"/invoices/{coasters.pk}/document/"),
        ("customer", brew.get_absolute_url()),
        ("files-to-review", "/portal/review/"),
        ("online-payments", "/settings/online-payments/"),
        ("reports", "/reports/"),
        ("floor", "/floor/"),
    ]
    # 3D models (0.11.x): the sheet metal bracket's flat pattern; the viewer is
    # opened from its job's files (see main).
    from trammel.files.services import attachments_for

    brackets = Job.objects.filter(tags__contains=["models"]).first()
    link = next(iter(attachments_for(brackets)), None) if brackets is not None else None
    if link is not None and link.document.current and link.document.current.blob.flat:
        staff.append(("flat-pattern", f"/files/{link.document.pk}/flat/"))
    portal = {
        cafe: [("portal-home", "/portal/"), ("portal-quote", f"/portal/quotes/{menu.pk}/")],
        grace: [
            ("portal-job", f"/portal/jobs/{table.pk}/"),
            ("portal-invoice", f"/portal/invoices/{deposit.pk}/"),
        ],
    }
    return staff, portal


def viewer(page, out, job_url: str | None) -> None:
    """The 3D viewer, opened on the demo's sheet metal bracket from its job's
    files, showing its edges. (The job is looked up beforehand: Django won't
    query the database inside Playwright's event loop.)"""
    if job_url is None:
        return
    page.goto(f"{BASE}{job_url}?tab=files", wait_until="networkidle")
    page.evaluate(HIDE)
    page.click("a[data-model]")
    page.wait_for_selector("dialog.model-viewer[open]")
    page.wait_for_selector(".mv-status[hidden]", state="attached", timeout=30000)
    page.check("dialog.model-viewer >> text=Edges")
    page.wait_for_timeout(600)
    page.screenshot(path=str(out / "model-viewer.png"))
    print("model-viewer: the bracket in the 3D viewer")
    page.keyboard.press("Escape")


def main() -> None:
    import django

    django.setup()
    from playwright.sync_api import sync_playwright

    from trammel.accounts.models import User
    from trammel.core import audit
    from trammel.portal import services as portal_services

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    email = os.environ["TRAMMEL_SHOTS_EMAIL"]
    password = os.environ["TRAMMEL_SHOTS_PASSWORD"]
    admin = User.objects.get(email=email)
    staff, portal = pages()
    from trammel.jobs.models import Job

    brackets = Job.objects.filter(tags__contains=["models"]).first()
    bracket_url = brackets.get_absolute_url() if brackets else None
    with audit.acting_as(admin, source="cli"):
        codes = {c: portal_services.make_code(c, user=admin)[0] for c in portal}

    def shot(page, name, path):
        page.goto(BASE + path, wait_until="networkidle")
        page.evaluate(HIDE)
        page.wait_for_timeout(400)
        page.screenshot(path=str(out / f"{name}.png"))
        print(f"{name}: {path}")

    def sign_in(page):
        page.goto(BASE + "/accounts/login/")
        page.fill('input[name="email"]', email)
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

    with sync_playwright() as p:
        # The 3D viewer needs WebGL, drawn in software where there's no GPU.
        browser = p.chromium.launch(
            executable_path=os.environ.get("TRAMMEL_SHOTS_CHROME") or None,
            args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="light")
        page = context.new_page()
        sign_in(page)
        for name, path in staff:
            shot(page, name, path)
        viewer(page, out, bracket_url)
        context.close()

        for customer, customer_pages in portal.items():
            context = browser.new_context(
                viewport={"width": 1280, "height": 860}, color_scheme="light"
            )
            page = context.new_page()
            page.goto(BASE + "/portal/sign-in/access-code/")
            page.fill('input[name="code"]', codes[customer])
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")
            for name, path in customer_pages:
                shot(page, name, path)
            context.close()

        phone = browser.new_context(
            viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True
        )
        page = phone.new_page()
        sign_in(page)
        shot(page, "floor-phone", "/floor/")
        phone.close()
        browser.close()


if __name__ == "__main__":
    main()
