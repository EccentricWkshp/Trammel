"""
Built-in quote estimators (FR-QUO-2, SPEC §4.5).

Every estimator takes structured inputs and returns one priced quote line with a
visible cost breakdown. Rates come from the quote revision, which copied them
from Shop rates and machines and may override them (FR-QUO-3).

Pricing rules, the same for every estimator:

* Materials: quantity × the material's cost per unit, plus the consumables
  allowance, then the material markup (the material's own, else the quote's).
* Labor: hours × the labor rate.
* Machine time: hours × the machine's hourly rate, costed at its calculated cost
  rate (the machine rate calculator).
* Overhead: (labor + machine hours) × the overhead rate.
* Per-piece inputs are multiplied by the quantity; setup time is counted once.

Adding an estimator means a subclass here and an entry in `ESTIMATORS`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django import forms

from trammel.core import units
from trammel.core.money import round_money
from trammel.shop.models import Machine, Material

D = Decimal
ZERO = D("0")
HOUR = D("60")


@dataclass
class Rates:
    labor: Decimal
    overhead: Decimal
    markup: Decimal  # percent
    consumables: Decimal  # percent

    @classmethod
    def of(cls, revision) -> Rates:
        return cls(
            revision.labor_rate,
            revision.overhead_rate,
            revision.material_markup,
            revision.consumables_pct,
        )


@dataclass
class Result:
    description: str
    quantity: Decimal
    line_total: Decimal = ZERO
    cost_total: Decimal = ZERO
    breakdown: list[dict] = field(default_factory=list)
    split: dict = field(default_factory=dict)

    @property
    def unit_price(self) -> Decimal:
        return (self.line_total / self.quantity).quantize(D("0.0001")) if self.quantity else ZERO

    @property
    def unit_cost(self) -> Decimal:
        return (self.cost_total / self.quantity).quantize(D("0.0001")) if self.quantity else ZERO


class _Builder:
    """Accumulates priced components into a Result."""

    def __init__(self, description: str, quantity: Decimal, rates: Rates):
        self.result = Result(description, quantity)
        self.rates = rates
        self.hours = ZERO

    def _add(self, category: str, label: str, detail: str, cost: Decimal, price: Decimal):
        cost, price = round_money(cost), round_money(price)
        r = self.result
        r.breakdown.append(
            {
                "label": label,
                "detail": detail,
                "cost": str(cost),
                "price": str(price),
                "category": category,
            }
        )
        r.cost_total += cost
        r.line_total += price
        r.split[category] = str(D(r.split.get(category, "0")) + price)

    def material(self, material: Material, qty: Decimal, unit: str, label: str = "Material"):
        if not material or not qty:
            return
        base = qty * material.cost_per(unit)
        cost = base * (1 + self.rates.consumables / 100)
        markup = material.markup if material.markup is not None else self.rates.markup
        price = cost * (1 + markup / 100)
        detail = f"{qty.normalize():f} {unit} of {material}"
        if self.rates.consumables:
            detail += f" + {self.rates.consumables.normalize():f}% consumables"
        detail += f", {markup.normalize():f}% markup"
        self._add("materials", label, detail, cost, price)

    def labor(self, hours: Decimal, label: str = "Labor"):
        if not hours:
            return
        amount = hours * self.rates.labor
        self.hours += hours
        self._add(
            "labor",
            label,
            f"{hours.normalize():f} h × {self.rates.labor.normalize():f}/h",
            amount,
            amount,
        )

    def machine(self, machine: Machine, hours: Decimal, label: str = "Machine time"):
        if not machine or not hours:
            return
        amount = hours * machine.hourly_rate
        self.hours += hours
        self._add(
            "machine",
            f"{label} — {machine}",
            f"{hours.normalize():f} h × {machine.hourly_rate.normalize():f}/h",
            hours * machine.cost_rate,
            amount,
        )

    def other(self, label: str, amount: Decimal, detail: str = ""):
        if amount:
            self._add("other", label, detail, amount, amount)

    def finish(self) -> Result:
        if self.rates.overhead and self.hours:
            amount = self.hours * self.rates.overhead
            self._add(
                "other",
                "Shop overhead",
                f"{self.hours.normalize():f} h × {self.rates.overhead.normalize():f}/h",
                amount,
                amount,
            )
        return self.result


def _materials(*categories):
    qs = Material.objects.filter(is_active=True)
    return qs.filter(category__in=categories) if categories else qs


def _machines(*kinds):
    qs = Machine.objects.filter(is_active=True)
    return qs.filter(kind__in=kinds) if kinds else qs


def _dec(**kw):
    kw.setdefault("required", False)
    kw.setdefault("min_value", 0)
    kw.setdefault("initial", 0)
    return forms.DecimalField(max_digits=12, decimal_places=4, **kw)


class Estimator:
    key = ""
    name = ""
    description = ""

    def form_class(self) -> type[forms.Form]:
        raise NotImplementedError

    def estimate(self, data: dict, rates: Rates) -> Result:
        raise NotImplementedError

    @staticmethod
    def clean_inputs(data: dict) -> dict:
        """Cleaned form data → JSON-safe inputs, stored on the line."""
        out = {}
        for key, value in data.items():
            if hasattr(value, "pk"):
                out[key] = str(value.pk)
            elif isinstance(value, Decimal):
                out[key] = str(value)
            else:
                out[key] = value
        return out


class GenericEstimator(Estimator):
    key = "generic"
    name = "Generic (materials + labor + machine time)"
    description = "For woodworking, metal fabrication and anything without its own estimator."

    def form_class(self):
        class GenericForm(forms.Form):
            description = forms.CharField(max_length=300)
            quantity = _dec(label="Quantity (pieces)", min_value=1, initial=1, required=True)
            material = forms.ModelChoiceField(_materials(), required=False)
            material_quantity = _dec(label="Material per piece")
            material_unit = forms.ChoiceField(
                choices=[("", "— material's own unit —"), *units.UNIT_CHOICES], required=False
            )
            other_materials = _dec(label="Other materials (cost, total)")
            labor_hours = _dec(label="Labor hours per piece")
            setup_hours = _dec(label="Setup / design hours (once)")
            machine = forms.ModelChoiceField(_machines(), required=False)
            machine_hours = _dec(label="Machine hours per piece")

        return GenericForm

    def estimate(self, data, rates):
        qty = data["quantity"]
        b = _Builder(data["description"], qty, rates)
        material = data.get("material")
        if material:
            unit = data.get("material_unit") or material.unit
            b.material(material, (data.get("material_quantity") or ZERO) * qty, unit)
        if other := data.get("other_materials"):
            price = other * (1 + rates.markup / 100)
            b._add(
                "materials",
                "Other materials",
                f"{other.normalize():f} + {rates.markup.normalize():f}% markup",
                other,
                price,
            )
        b.labor((data.get("labor_hours") or ZERO) * qty)
        b.labor(data.get("setup_hours") or ZERO, "Setup / design")
        b.machine(data.get("machine"), (data.get("machine_hours") or ZERO) * qty)
        return b.finish()


class PrintEstimator(Estimator):
    key = "print3d"
    name = "3D printing"
    description = "Material by weight, print hours, machine rate and a failure allowance."

    def form_class(self):
        class PrintForm(forms.Form):
            description = forms.CharField(max_length=300)
            quantity = _dec(label="Quantity (pieces)", min_value=1, initial=1, required=True)
            material = forms.ModelChoiceField(_materials("filament"), label="Filament / resin")
            grams = _dec(label="Material per piece (g)", required=True)
            print_hours = _dec(label="Print hours per piece", required=True)
            machine = forms.ModelChoiceField(_machines("printer_3d"), label="Printer")
            failure_pct = _dec(label="Failure allowance (%)", initial=10, max_value=100)
            post_hours = _dec(label="Post-processing hours per piece")
            setup_hours = _dec(label="Setup / slicing hours (once)")

        return PrintForm

    def estimate(self, data, rates):
        qty = data["quantity"]
        allowance = 1 + (data.get("failure_pct") or ZERO) / 100
        b = _Builder(data["description"], qty, rates)
        b.material(
            data["material"],
            data["grams"] * qty * allowance,
            "g",
            label="Material (incl. failure allowance)",
        )
        b.machine(data["machine"], data["print_hours"] * qty * allowance, label="Print time")
        b.labor((data.get("post_hours") or ZERO) * qty, "Post-processing")
        b.labor(data.get("setup_hours") or ZERO, "Setup / slicing")
        return b.finish()


AREA_UNITS = [("sq_in", "in²"), ("sq_ft", "ft²"), ("sq_mm", "mm²"), ("sq_cm", "cm²")]


class LaserEstimator(Estimator):
    key = "laser"
    name = "Laser cutting / engraving"
    description = "Material by sheet or area, cut and engrave time, machine rate."

    def form_class(self):
        class LaserForm(forms.Form):
            description = forms.CharField(max_length=300)
            quantity = _dec(label="Quantity (pieces)", min_value=1, initial=1, required=True)
            material = forms.ModelChoiceField(
                _materials("sheet"), required=False, help_text="Leave blank for customer-supplied."
            )
            area = _dec(label="Material area per piece")
            area_unit = forms.ChoiceField(choices=AREA_UNITS, initial="sq_in")
            sheets = _dec(
                label="…or whole sheets (total)", help_text="Used instead of area when given."
            )
            waste_pct = _dec(label="Waste allowance (%)", initial=15, max_value=100)
            cut_minutes = _dec(label="Cut minutes per piece")
            engrave_minutes = _dec(label="Engrave minutes per piece")
            machine = forms.ModelChoiceField(_machines("laser"), label="Laser")
            labor_minutes = _dec(label="Handling / weeding minutes per piece")
            setup_minutes = _dec(label="Setup / design minutes (once)")

            def clean(self):
                data = super().clean()
                material = data.get("material")
                if (
                    material
                    and not data.get("sheets")
                    and data.get("area")
                    and material.sheet_area_sq_mm is None
                    and material.unit == "sheet"
                ):
                    self.add_error(
                        "area",
                        "This material has no sheet size; enter whole "
                        "sheets instead, or give the material its size.",
                    )
                return data

        return LaserForm

    def estimate(self, data, rates):
        qty = data["quantity"]
        b = _Builder(data["description"], qty, rates)
        material = data.get("material")
        if material:
            if data.get("sheets"):
                b.material(
                    material,
                    data["sheets"],
                    "sheet" if material.unit == "sheet" else material.unit,
                    label="Material (sheets)",
                )
            elif data.get("area"):
                waste = 1 + (data.get("waste_pct") or ZERO) / 100
                b.material(
                    material,
                    data["area"] * qty * waste,
                    data["area_unit"],
                    label="Material (incl. waste)",
                )
        minutes = ((data.get("cut_minutes") or ZERO) + (data.get("engrave_minutes") or ZERO)) * qty
        b.machine(data["machine"], minutes / HOUR, label="Laser time")
        b.labor((data.get("labor_minutes") or ZERO) * qty / HOUR, "Handling")
        b.labor((data.get("setup_minutes") or ZERO) / HOUR, "Setup / design")
        return b.finish()


LENGTH_UNITS = [("in", "in"), ("ft", "ft"), ("mm", "mm")]
SQUARE = {"in": "sq_in", "ft": "sq_ft", "mm": "sq_mm"}


class CuttingEstimator(Estimator):
    key = "cutting"
    name = "Profile cutting (plasma, CNC, laser)"
    description = (
        "Parts cut from sheet or plate: material by the part's rectangle, cutting priced by "
        "machine time or per inch and per pierce."
    )

    def form_class(self):
        class CuttingForm(forms.Form):
            description = forms.CharField(max_length=300)
            quantity = _dec(label="Quantity (pieces)", min_value=1, initial=1, required=True)
            machine = forms.ModelChoiceField(_machines("plasma", "cnc", "laser"))
            material = forms.ModelChoiceField(
                _materials("sheet", "metal"),
                required=False,
                help_text="Leave blank for customer-supplied.",
            )
            length_unit = forms.ChoiceField(choices=LENGTH_UNITS, initial="in", label="Units")
            part_length = _dec(label="Part length", help_text="The rectangle it is cut from.")
            part_width = _dec(label="Part width")
            waste_pct = _dec(label="Waste allowance (%)", initial=15, max_value=100)
            cut_length = _dec(label="Cut length per piece", required=True)
            pierces = _dec(label="Pierces per piece", initial=1)
            pricing = forms.ChoiceField(
                label="Price cutting by",
                choices=[("time", "Machine time"), ("length", "Per inch and per pierce")],
                initial="time",
            )
            feed_rate = _dec(
                label="Feed rate (units per minute)",
                help_text="Needed for machine time; also costs per-inch pricing.",
            )
            pierce_seconds = _dec(label="Seconds per pierce", initial=1)
            price_per_length = _dec(label="Price per unit of cut length")
            price_per_pierce = _dec(label="Price per pierce")
            handling_minutes = _dec(label="Handling / deburring minutes per piece")
            cad_hours = _dec(label="CAD/CAM and setup hours (once)")

            def clean(self):
                data = super().clean()
                if data.get("pricing") == "time" and not data.get("feed_rate"):
                    self.add_error("feed_rate", "Machine-time pricing needs the feed rate.")
                if data.get("pricing") == "length" and not (
                    data.get("price_per_length") or data.get("price_per_pierce")
                ):
                    self.add_error("price_per_length", "Give a price per unit of length or pierce.")
                if data.get("material") and not (
                    data.get("part_length") and data.get("part_width")
                ):
                    self.add_error("part_length", "Give the part's length and width to price it.")
                return data

        return CuttingForm

    def estimate(self, data, rates):
        qty = data["quantity"]
        unit = data.get("length_unit") or "in"
        b = _Builder(data["description"], qty, rates)
        material = data.get("material")
        if material and data.get("part_length") and data.get("part_width"):
            waste = 1 + (data.get("waste_pct") or ZERO) / 100
            area = data["part_length"] * data["part_width"] * qty * waste
            b.material(material, area, SQUARE[unit], label="Material (incl. waste)")

        machine = data["machine"]
        cut = data["cut_length"] * qty
        pierces = (data.get("pierces") or ZERO) * qty
        feed = data.get("feed_rate") or ZERO
        minutes = (cut / feed if feed else ZERO) + pierces * (
            data.get("pierce_seconds") or ZERO
        ) / 60
        hours = minutes / HOUR
        if data.get("pricing") == "length":
            per_len = data.get("price_per_length") or ZERO
            per_pierce = data.get("price_per_pierce") or ZERO
            price = cut * per_len + pierces * per_pierce
            detail = f"{cut.normalize():f} {unit} × {per_len.normalize():f}"
            if pierces:
                detail += f" + {pierces.normalize():f} pierces × {per_pierce.normalize():f}"
            b.hours += hours
            b._add("machine", f"Cutting — {machine}", detail, hours * machine.cost_rate, price)
        else:
            b.machine(machine, hours, label="Cutting")
        b.labor((data.get("handling_minutes") or ZERO) * qty / HOUR, "Handling / deburring")
        b.labor(data.get("cad_hours") or ZERO, "CAD/CAM and setup")
        return b.finish()


ESTIMATORS: dict[str, Estimator] = {
    e.key: e for e in (GenericEstimator(), PrintEstimator(), LaserEstimator(), CuttingEstimator())
}
