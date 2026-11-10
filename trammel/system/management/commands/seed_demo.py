"""Fill a test instance with realistic sample data, through the real services.

Every record is created the way the application creates it — lifecycle moves,
document numbers, audit entries and all — so the result behaves exactly like a
shop that has been using Trammel for a few weeks. Never run this on real data.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from trammel.core import audit
from trammel.core.access import SYSTEM

MARKER = "demo.trammel.test"  # every demo customer's email domain


def at(days_ago: int, hour: int = 9, minute: int = 0) -> dt.datetime:
    day = timezone.localdate() - dt.timedelta(days=days_ago)
    return timezone.make_aware(dt.datetime.combine(day, dt.time(hour, minute)))


class Command(BaseCommand):
    help = "Fill a TEST instance with sample customers, jobs, quotes, work orders and invoices."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")

    def handle(self, *args, yes=False, **options):
        from trammel.accounts.models import User
        from trammel.customers.models import Customer

        if Customer.all_objects.filter(email__endswith=MARKER).exists():
            raise CommandError("Demo data is already loaded.")
        admin = User.objects.filter(is_admin=True, is_active=True).order_by("date_joined").first()
        if admin is None:
            raise CommandError("Complete the setup page first; demo data needs an admin.")
        if not yes and input("This adds sample data to THIS instance. Type DEMO: ") != "DEMO":
            raise CommandError("Cancelled.")
        with audit.acting_as(admin, source="cli"), transaction.atomic():
            Builder(admin, self.stdout).run()
        self.stdout.write(self.style.SUCCESS("Demo data loaded."))


class Builder:
    def __init__(self, admin, out):
        self.admin = admin
        self.out = out

    def say(self, text):
        self.out.write(f"  {text}")

    # ------------------------------------------------------------------
    def run(self):
        self.business()
        self.staff()
        self.shop()
        self.tax()
        self.catalog()
        self.customers()
        self.jobs()
        self.direct_sale()

    # ------------------------------------------------------------------
    def business(self):
        from trammel.core.models import BusinessProfile, ShopRates

        b = BusinessProfile.load()
        for field, value in {
            "phone": "(253) 555-0142",
            "email": "shop@demo.trammel.test",
            "address_line1": "1120 Pacific Ave",
            "city": "Tacoma",
            "region": "WA",
            "postal_code": "98402",
            "tax_registration_numbers": "WA UBI: 604-555-123",
        }.items():
            if not getattr(b, field):
                setattr(b, field, value)
        if b.timezone == "UTC":
            b.timezone = "America/Los_Angeles"
        b.save()
        r = ShopRates.load()
        if not r.labor_hourly_rate:
            r.labor_hourly_rate, r.labor_daily_rate = D("75"), D("540")
            r.shop_overhead_rate, r.default_material_markup = D("12"), D("35")
            r.consumables_allowance, r.minimum_job_charge = D("5"), D("50")
            r.save()
        self.say("business details and shop rates")

    def staff(self):
        from trammel.accounts.models import Role, User
        from trammel.core.modules import all_permissions

        hand, _ = Role.objects.get_or_create(
            name="Shop hand",
            defaults={
                "description": "Works jobs on the floor.",
                "permissions": [
                    "customers.view",
                    "jobs.view",
                    "quotes.view",
                    "workorders.view",
                    "workorders.edit",
                    "time.view",
                    "time.track",
                    "files.view",
                    "files.upload",
                    "shop.view",
                    "catalog.view",
                ],
            },
        )
        manager, _ = Role.objects.get_or_create(
            name="Manager",
            defaults={
                "description": "Runs the office: quotes, invoices, scheduling.",
                "permissions": [
                    p for p in all_permissions() if not p.startswith(("core.manage", "accounts."))
                ],
            },
        )
        self.hand = User.objects.filter(email="sam@demo.trammel.test").first()
        if self.hand is None:
            self.hand = User.objects.create_user(
                "sam@demo.trammel.test", None, full_name="Sam Ortiz", labor_cost_rate=D("32")
            )
            self.hand.roles.add(hand)
            office = User.objects.create_user(
                "riley@demo.trammel.test", None, full_name="Riley Chen"
            )
            office.roles.add(manager)
        self.say(
            "roles “Shop hand” and “Manager”, users Sam Ortiz and Riley Chen "
            "(no passwords; issue a password link from Users to sign in as them)"
        )

    def shop(self):
        from trammel.shop.models import Machine, Material, Vendor

        def machine(name, kind, rate, **calculator):
            return Machine.objects.get_or_create(
                name=name, defaults={"kind": kind, "hourly_rate": D(rate), **calculator}
            )[0]

        # Rate calculator inputs, so job costing shows each machine's real margin.
        self.laser = machine(
            "Epilog Fusion 32",
            "laser",
            "85",
            purchase_cost=D("38000"),
            salvage_value=D("8000"),
            lifespan_hours=15000,
            maintenance_cost=D("250"),
            maintenance_interval_hours=500,
            power_kw=D("2.5"),
            wear_parts=[
                {"name": "Laser tube / source", "cost": "4500", "life_hours": 8000},
                {"name": "Lenses and mirrors", "cost": "300", "life_hours": 1500},
            ],
            failure_pct=D("3"),
            markup_pct=D("300"),
        )
        self.printer = machine(
            "Prusa MK4",
            "printer_3d",
            "6",
            purchase_cost=D("1100"),
            lifespan_hours=6000,
            maintenance_cost=D("40"),
            maintenance_interval_hours=750,
            power_kw=D("0.12"),
            wear_parts=[{"name": "Nozzle / hotend", "cost": "35", "life_hours": 800}],
            failure_pct=D("10"),
            markup_pct=D("200"),
        )
        self.cnc = machine(
            "ShopBot Desktop",
            "cnc",
            "65",
            purchase_cost=D("14000"),
            salvage_value=D("3000"),
            lifespan_hours=12000,
            power_kw=D("3.5"),
            consumables_per_hour=D("1.50"),
            wear_parts=[{"name": "Router bits", "cost": "60", "life_hours": 40}],
            failure_pct=D("3"),
            markup_pct=D("400"),
        )
        machine("SawStop table saw", "saw", "15")

        def vendor(name, **kw):
            return Vendor.objects.get_or_create(name=name, defaults=kw)[0]

        self.edensaw = vendor(
            "Edensaw",
            contact_name="Counter sales",
            phone="(360) 555-0142",
            website="https://example.com/edensaw",
            account_number="TR-20931",
            payment_terms="Net 30",
            lead_time_days=2,
        )
        self.crosscut = vendor(
            "Crosscut Hardwoods",
            phone="(206) 555-0188",
            payment_terms="Credit card",
            lead_time_days=1,
        )
        self.filament_vendor = vendor(
            "Printed Solid", website="https://example.com/filament", lead_time_days=4
        )

        def material(name, **kw):
            return Material.objects.get_or_create(name=name, defaults=kw)[0]

        self.ply = material(
            "Baltic birch plywood",
            category="sheet",
            thickness="1/8 in",
            unit="sheet",
            unit_cost=D("18.50"),
            sheet_width=D("24"),
            sheet_length=D("12"),
            sheet_dimension_unit="in",
            vendor=self.edensaw,
        )
        self.acrylic = material(
            "Cast acrylic, clear",
            category="sheet",
            thickness="3 mm",
            unit="sheet",
            unit_cost=D("24.00"),
            sheet_width=D("24"),
            sheet_length=D("18"),
            sheet_dimension_unit="in",
        )
        self.walnut = material(
            "Black walnut",
            category="lumber",
            thickness="4/4",
            unit="board_ft",
            unit_cost=D("14.25"),
            vendor=self.crosscut,
            url="https://example.com/hardwoods/black-walnut-4-4",
        )
        self.maple = material(
            "Hard maple", category="lumber", thickness="8/4", unit="board_ft", unit_cost=D("9.80")
        )
        self.pla = material(
            "PLA filament, black",
            category="filament",
            unit="kg",
            unit_cost=D("22.00"),
            markup=D("50"),
            vendor=self.filament_vendor,
        )
        material("Wood screws #8 x 1-1/4", category="hardware", unit="each", unit_cost=D("0.06"))
        material("Danish oil", category="finish", unit="l", unit_cost=D("38.00"))
        self.say("4 machines and 7 materials")

    def tax(self):
        from trammel.tax.models import Boundary, Jurisdiction, TaxCategory, TaxRate

        TaxCategory.objects.get_or_create(
            name="Retail sale",
            defaults={
                "taxable": True,
                "is_default": True,
                "gross_receipts_classification": "Retailing",
            },
        )
        TaxCategory.objects.get_or_create(
            name="Resale (reseller permit)",
            defaults={"taxable": False, "gross_receipts_classification": "Wholesaling"},
        )
        wa, _ = Jurisdiction.objects.get_or_create(
            code="WA-BO", defaults={"name": "Washington B&O", "level": "state", "region": "WA"}
        )
        for cls, rate in (("Retailing", "0.471"), ("Wholesaling", "0.484")):
            TaxRate.objects.get_or_create(
                jurisdiction=wa,
                kind="gross_receipts",
                classification=cls,
                defaults={"rate": D(rate), "effective_from": dt.date(2020, 1, 1)},
            )
        if not Boundary.objects.filter(
            zip_low__lte="984020000", zip_high__gte="984029999"
        ).exists():
            # No rate files imported: a manual Tacoma rate so invoices can be issued.
            tac, _ = Jurisdiction.objects.get_or_create(code="2717", defaults={"name": "Tacoma"})
            TaxRate.objects.get_or_create(
                jurisdiction=tac,
                kind="collected",
                defaults={"rate": D("10.5"), "effective_from": dt.date(2025, 1, 1)},
            )
            for low, high in (("98402", "98409"), ("98101", "98199"), ("98001", "98099")):
                Boundary.objects.create(
                    zip_low=low + "0000",
                    zip_high=high + "9999",
                    jurisdiction_codes=["2717"],
                    location_code="2717",
                    source="manual",
                )
        self.say("tax categories, B&O rates (and a manual rate if no SST files are loaded)")

    def catalog(self):
        from trammel.catalog.models import Product

        def product(sku, name, kind, price, cost, unit="each"):
            return Product.objects.get_or_create(
                sku=sku,
                defaults={
                    "name": name,
                    "kind": kind,
                    "price": D(price),
                    "cost": D(cost),
                    "unit": unit,
                },
            )[0]

        self.board = product("CB-WAL-12", "Walnut cutting board, 12 × 16", "made", "85", "28")
        self.coasters = product("CO-SET-4", "Engraved coaster set (4)", "made", "36", "9")
        product("SIGN-CUST", "Custom engraved sign", "made", "120", "35")
        self.oil = product("FIN-OIL", "Board butter, 4 oz", "resold", "12", "5.50")
        product("DES-HR", "Design time", "service", "75", "0", unit="hr")
        self.say("5 products")

    def customers(self):
        from trammel.customers.models import Address, Contact, Customer

        def customer(
            name,
            *,
            company="",
            kind="individual",
            phone,
            city,
            postal,
            line1,
            contact=None,
            exempt="",
        ):
            c = Customer.objects.create(
                name=name,
                company=company,
                kind=kind,
                phone=phone,
                email=f"{name.split()[0].lower()}@{MARKER}",
                tax_exempt=bool(exempt),
                tax_exempt_number=exempt,
                created_by=self.admin,
            )
            Address.objects.create(
                customer=c,
                label="Main",
                line1=line1,
                city=city,
                region="WA",
                postal_code=postal,
                is_default_billing=True,
                is_default_shipping=True,
            )
            if contact:
                Contact.objects.create(
                    customer=c,
                    name=contact[0],
                    role=contact[1],
                    email=f"{contact[0].split()[0].lower()}@{MARKER}",
                    is_primary=True,
                )
            audit.record_create(c)
            return c

        self.c_gh = customer(
            "Grace Hollis",
            phone="(253) 555-0101",
            city="Tacoma",
            postal="98403",
            line1="412 N Tacoma Ave",
        )
        self.c_brew = customer(
            "Foundry Brewing",
            company="Foundry Brewing LLC",
            kind="company",
            phone="(253) 555-0177",
            city="Tacoma",
            postal="98402",
            line1="1501 Broadway",
            contact=("Dana Price", "Owner"),
        )
        self.c_cafe = customer(
            "Lantern Café",
            company="Lantern Café",
            kind="company",
            phone="(206) 555-0123",
            city="Seattle",
            postal="98101",
            line1="88 Pike St",
            contact=("Marco Ruiz", "Manager"),
        )
        self.c_maker = customer(
            "Northwest Maker Supply",
            company="Northwest Maker Supply Inc.",
            kind="company",
            phone="(253) 555-0190",
            city="Kent",
            postal="98032",
            line1="20400 84th Ave S",
            contact=("Priya Shah", "Purchasing"),
            exempt="RS-604-555-001",
        )
        self.c_tom = customer(
            "Tom Becker",
            phone="(253) 555-0133",
            city="Tacoma",
            postal="98405",
            line1="2210 S 12th St",
        )
        self.c_ava = customer(
            "Ava Lindqvist",
            phone="(206) 555-0166",
            city="Seattle",
            postal="98103",
            line1="3600 Fremont Ave N",
        )
        self.say("6 customers with addresses and contacts")

    # ------------------------------------------------------------------
    def _job(self, customer, title, scope, *, due, promised=None, tags=(), priority=1):
        from trammel.jobs import services as jobs
        from trammel.jobs.models import Job

        today = timezone.localdate()
        return jobs.create_job(
            Job(
                customer=customer,
                title=title,
                description=scope,
                tags=list(tags),
                due_date=today + dt.timedelta(days=due),
                promised_date=today + dt.timedelta(days=promised) if promised is not None else None,
                target_completion=today + dt.timedelta(days=due - 2),
                priority=priority,
            )
        )

    def _quote(self, job, lines, *, deposit=None, send=True):
        from trammel.quotes import services as quotes
        from trammel.quotes.forms import LINE_FORMS

        rev = quotes.create_quote(job)
        for kind, data in lines:
            if kind == "estimate":
                key, inputs = data
                from trammel.quotes.estimators import ESTIMATORS

                form = ESTIMATORS[key].form_class()(inputs)
                assert form.is_valid(), form.errors
                quotes.save_estimate(quotes.estimate_line(rev, key, form.cleaned_data))
            else:
                form = LINE_FORMS[kind](data)
                assert form.is_valid(), form.errors
                quotes.save_line(form.build(rev))
        if deposit:
            before = audit.snapshot(rev)
            rev.refresh_from_db()
            rev.deposit_kind, rev.deposit_value = "percent", D(deposit)
            rev.fulfillment = "delivery"
            rev.shipping = D("85")
            quotes.update_revision(rev, before=before)
        if send:
            quotes.send(rev, actor=self.admin)
        rev.refresh_from_db()
        return rev

    def _approve(self, rev, name):
        from trammel.quotes import services as quotes

        quotes.approve(
            rev,
            actor=self.admin,
            approved_by_name=name,
            how="email",
            accept_agreement=True,
            user=self.admin,
        )
        rev.refresh_from_db()

    def _pay(self, invoice, amount=None, days_ago=1, method="card"):
        from trammel.billing import services as billing

        invoice.refresh_from_db()
        amount = invoice.balance if amount is None else D(amount)
        billing.record_payment(
            invoice.customer,
            amount=amount,
            method=method,
            received_on=timezone.localdate() - dt.timedelta(days=days_ago),
            reference="demo",
            allocations={invoice: amount},
        )

    def _work(self, job, rev, *, hours, material=None, done=True, note=""):
        from trammel.timetracking import services as time
        from trammel.workorders import services as wos

        job.refresh_from_db()
        wo = wos.create(job, revision=rev)
        time.manual(
            wo,
            kind="labor",
            minutes=D(hours * 60),
            started_at=at(3, 8),
            user=self.hand,
            note="Build",
        )
        if material:
            wos.record_material(
                wo, material=material[0], quantity=D(material[1]), unit="", user=self.hand
            )
        if note:
            wos.add_note(wo, text=note, user=self.hand)
        if done:
            for op in wo.operations.all():
                wos.set_operation(op, "done", user=self.hand)
        wo.refresh_from_db()
        return wo

    def jobs(self):
        from trammel.billing import services as billing
        from trammel.jobs import services as jobs
        from trammel.workorders import services as wos

        ply_sign = (
            "estimate",
            (
                "laser",
                {
                    "description": "Engraved birch sign, 18 × 12 in",
                    "quantity": "1",
                    "material": str(self.ply.pk),
                    "area": "216",
                    "area_unit": "sq_in",
                    "waste_pct": "15",
                    "cut_minutes": "6",
                    "engrave_minutes": "35",
                    "machine": str(self.laser.pk),
                    "labor_minutes": "20",
                    "setup_minutes": "45",
                },
            ),
        )

        # 1. Proposal: just started.
        self._job(
            self.c_ava,
            "Built-in bookshelf",
            "Floor-to-ceiling walnut bookshelf for "
            "a 9 ft alcove; customer is sending measurements.",
            due=45,
            tags=["furniture"],
        )

        # 2. Quoted: sent, waiting on the customer.
        j = self._job(
            self.c_cafe,
            "Menu boards",
            "Three laser-engraved menu boards with hanging hardware.",
            due=21,
            promised=24,
            tags=["laser", "signage"],
        )
        self._quote(
            j,
            [
                ply_sign,
                (
                    "adhoc",
                    {
                        "description": "Hanging hardware kit",
                        "quantity": "3",
                        "unit": "each",
                        "unit_price": "14",
                        "unit_cost": "4",
                    },
                ),
            ],
        )

        # 3. Approved with a 50 % deposit, deposit paid; ready to schedule.
        j = self._job(
            self.c_brew,
            "Taproom tap handles",
            "24 custom tap handles: walnut blanks with engraved logo, 3D-printed ferrules.",
            due=14,
            promised=14,
            tags=["laser", "3d-print"],
            priority=2,
        )
        rev = self._quote(
            j,
            [
                (
                    "estimate",
                    (
                        "print3d",
                        {
                            "description": "Printed ferrules",
                            "quantity": "24",
                            "material": str(self.pla.pk),
                            "grams": "18",
                            "print_hours": "0.75",
                            "machine": str(self.printer.pk),
                            "failure_pct": "10",
                            "post_hours": "0.05",
                            "setup_hours": "0.5",
                        },
                    ),
                ),
                ("material", {"material": str(self.walnut.pk), "quantity": "9"}),
                ("labor", {"description": "Turn, sand and finish handles", "quantity": "10"}),
            ],
            deposit="50",
        )
        self._approve(rev, "Dana Price")
        self._pay(j.invoices.get(kind="deposit"), days_ago=2)

        # 4. In progress: work order running, waiting on materials.
        j = self._job(
            self.c_gh,
            "Walnut dining table",
            "72 × 38 in walnut dining table with breadboard ends, Danish oil finish.",
            due=4,
            promised=6,
            tags=["furniture"],
            priority=3,
        )
        rev = self._quote(
            j,
            [
                ("material", {"material": str(self.walnut.pk), "quantity": "42"}),
                ("labor", {"description": "Mill, glue up, joinery and finish", "quantity": "28"}),
                (
                    "machine",
                    {
                        "machine": str(self.cnc.pk),
                        "quantity": "1.5",
                        "description": "CNC breadboard mortises",
                    },
                ),
            ],
            deposit="30",
        )
        self._approve(rev, "Grace Hollis")
        self._pay(j.invoices.get(kind="deposit"), days_ago=10, method="check")
        wo = self._work(
            j,
            rev,
            hours=16,
            material=(self.walnut, "38"),
            done=False,
            note="Top glued up and flattened. Breadboard stock needs re-sawing.",
        )
        wos.transition(
            wo, "waiting", actor=self.admin, reason="Second walnut order", waiting_on="materials"
        )

        # 5. Completed and invoiced; invoice overdue.
        j = self._job(
            self.c_tom,
            "Shop sign",
            "Double-sided acrylic shop sign.",
            due=-20,
            tags=["laser", "signage"],
        )
        rev = self._quote(
            j,
            [
                (
                    "estimate",
                    (
                        "laser",
                        {
                            "description": "Acrylic sign, 24 × 18 in, both sides",
                            "quantity": "1",
                            "material": str(self.acrylic.pk),
                            "sheets": "2",
                            "area": "0",
                            "area_unit": "sq_in",
                            "cut_minutes": "12",
                            "engrave_minutes": "50",
                            "machine": str(self.laser.pk),
                            "setup_minutes": "30",
                        },
                    ),
                )
            ],
        )
        self._approve(rev, "Tom Becker")
        wo = self._work(j, rev, hours=2, material=(self.acrylic, "2"))
        wos.transition(wo, "complete", actor=self.admin)
        wos.confirm_materials(wo)
        wos.transition(wo, "closed", actor=self.admin)
        j.refresh_from_db()
        jobs.transition(j, "completed", actor=self.admin)
        inv = billing.create(
            j.customer,
            kind="final",
            job=j,
            invoice_date=timezone.localdate() - dt.timedelta(days=45),
        )
        billing.add_from_quote(inv, rev)
        billing.issue(inv, actor=self.admin)

        # 6. Closed: fully paid, with a reseller (tax exempt) customer.
        j = self._job(
            self.c_maker,
            "Wholesale coaster sets",
            "120 engraved coaster sets for resale.",
            due=-10,
            tags=["laser", "wholesale"],
        )
        rev = self._quote(
            j,
            [
                (
                    "product",
                    {"product": str(self.coasters.pk), "quantity": "120", "discount_pct": "40"},
                )
            ],
        )
        self._approve(rev, "Priya Shah")
        wo = self._work(j, rev, hours=9, material=(self.ply, "30"))
        wos.transition(wo, "complete", actor=self.admin)
        wos.confirm_materials(wo)
        wos.transition(wo, "closed", actor=self.admin)
        j.refresh_from_db()
        jobs.transition(j, "completed", actor=self.admin)
        inv = billing.create(j.customer, kind="final", job=j)
        billing.add_from_quote(inv, rev)
        inv = billing.issue(inv, actor=self.admin)
        self._pay(inv, days_ago=3, method="bank")
        jobs.transition(j, "closed", actor=self.admin)

        # 7. On hold.
        j = self._job(
            self.c_brew, "Beer flight paddles", "40 maple flight paddles.", due=30, tags=["cnc"]
        )
        self._quote(
            j,
            [
                ("material", {"material": str(self.maple.pk), "quantity": "14"}),
                ("machine", {"machine": str(self.cnc.pk), "quantity": "3"}),
            ],
            send=False,
        )
        jobs.transition(j, "on_hold", actor=self.admin, reason="Waiting on final logo artwork")

        # 8. Internal: a shop improvement, no quote or invoice, with sub-operations.
        from trammel.customers.models import Customer

        j = self._job(
            Customer.internal(),
            "Outfeed table for the table saw",
            "Mobile outfeed table at saw height, with storage for jigs underneath.",
            due=20,
            tags=["shop"],
            priority=0,
        )
        jobs.transition(j, "approved", actor=self.admin)
        wo = wos.create(j, title="Build the outfeed table")
        case = wos.add_operation(wo, name="Case")
        cut = wos.add_operation(wo, name="Cut the plywood panels", parent=case)
        wos.add_operation(wo, name="Assemble and square the case", parent=case)
        top = wos.add_operation(wo, name="Top")
        wos.add_operation(wo, name="Laminate the top", parent=top)
        wos.add_operation(wo, name="Rout the miter slots", parent=top)
        wos.add_operation(wo, name="Casters and finish")
        wos.set_operation(cut, "done", user=self.hand)
        self.say(
            "8 jobs across the lifecycle, with quotes, work orders, time, materials, "
            "deposits and invoices, and one internal shop job"
        )

    def direct_sale(self):
        from trammel.billing import services as billing

        inv = billing.create(self.c_gh, kind="direct_sale")
        billing.add_line(
            inv,
            description=self.board.name,
            quantity=1,
            unit_price=self.board.price,
            product=self.board,
            category="product",
        )
        billing.add_line(
            inv,
            description=self.oil.name,
            quantity=2,
            unit_price=self.oil.price,
            product=self.oil,
            category="product",
        )
        inv.fulfillment = "pickup"
        inv.save()
        inv = billing.issue(inv, actor=SYSTEM)
        self._pay(inv, days_ago=0, method="cash")
        self.say("1 counter sale, paid in cash")
