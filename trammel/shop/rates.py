"""What a machine costs to run per hour, and a suggested rate to charge for it.

The same arithmetic runs in the browser (static/shop/machine_rate.js) for a live
preview while the form is filled in; this module is the one that counts.

    ownership    (purchase price − resale value) ÷ expected life in hours
    maintenance  cost of a service ÷ hours between services
    wear parts   Σ part cost ÷ part life in hours
    power        average draw in kW × electricity price per kWh
    consumables  gas, wire, air and the like, per hour
    fixed        this machine's yearly fixed costs ÷ hours run per year
    ─────────────────────────────────────────────
    running cost × (1 + failure allowance) = cost per hour
    cost per hour × (1 + markup)           = suggested rate
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")


@dataclass
class Row:
    label: str
    detail: str
    per_hour: Decimal


@dataclass
class Breakdown:
    rows: list[Row] = field(default_factory=list)
    running: Decimal = ZERO
    failure_pct: Decimal = ZERO
    failure: Decimal = ZERO
    cost: Decimal = ZERO
    markup_pct: Decimal = ZERO
    suggested: Decimal = ZERO
    notes: list[str] = field(default_factory=list)

    @property
    def has_inputs(self) -> bool:
        return bool(self.rows)


def _d(value) -> Decimal:
    if value in (None, ""):
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _num(value: Decimal) -> str:
    return f"{value.quantize(CENT, ROUND_HALF_UP):,}" if value % 1 else f"{value:,.0f}"


def calculate(inputs: dict, electricity_rate: Decimal) -> Breakdown:
    """`inputs` holds the Machine calculator fields by name (values may be None)."""
    b = Breakdown()
    purchase, salvage = _d(inputs.get("purchase_cost")), _d(inputs.get("salvage_value"))
    life = _d(inputs.get("lifespan_hours"))
    if purchase and life:
        b.rows.append(
            Row(
                "Ownership",
                f"({_num(purchase)} − {_num(salvage)} resale) ÷ {_num(life)} h"
                if salvage
                else f"{_num(purchase)} ÷ {_num(life)} h",
                max(ZERO, purchase - salvage) / life,
            )
        )
    elif purchase:
        b.notes.append("Enter the expected life in hours to spread the purchase price.")

    service = _d(inputs.get("maintenance_cost"))
    interval = _d(inputs.get("maintenance_interval_hours"))
    if service and interval:
        b.rows.append(
            Row("Maintenance", f"{_num(service)} every {_num(interval)} h", service / interval)
        )
    elif service:
        b.notes.append("Enter how many hours pass between services.")

    for part in inputs.get("wear_parts") or []:
        cost, hours = _d(part.get("cost")), _d(part.get("life_hours"))
        if cost and hours:
            name = part.get("name") or "Wear part"
            b.rows.append(Row(name, f"{_num(cost)} every {_num(hours)} h", cost / hours))

    kw = _d(inputs.get("power_kw"))
    if kw:
        if electricity_rate:
            b.rows.append(
                Row(
                    "Power",
                    f"{kw.normalize():f} kW × {electricity_rate.normalize():f}/kWh",
                    kw * electricity_rate,
                )
            )
        else:
            b.notes.append("Set the electricity price in Shop rates to include power.")

    consumables = _d(inputs.get("consumables_per_hour"))
    if consumables:
        b.rows.append(Row("Consumables", "per hour", consumables))

    fixed, yearly = _d(inputs.get("annual_fixed_costs")), _d(inputs.get("annual_hours"))
    if fixed and yearly:
        detail = f"{_num(fixed)} a year ÷ {_num(yearly)} h"
        b.rows.append(Row("Fixed costs", detail, fixed / yearly))
    elif fixed:
        b.notes.append("Enter the hours run per year to spread the yearly fixed costs.")

    b.running = sum((r.per_hour for r in b.rows), ZERO)
    b.failure_pct = _d(inputs.get("failure_pct"))
    b.failure = b.running * b.failure_pct / 100
    b.cost = (b.running + b.failure).quantize(CENT, ROUND_HALF_UP)
    b.markup_pct = _d(inputs.get("markup_pct"))
    b.suggested = (b.cost * (1 + b.markup_pct / 100)).quantize(CENT, ROUND_HALF_UP)
    return b


#: Starting points by machine type: typical wear parts (name, cost, life in hours)
#: and hints for the other inputs. They are suggestions to edit, not data.
PRESETS: dict[str, dict] = {
    "laser": {
        "wear_parts": [("Laser tube / source", 900, 2000), ("Lenses and mirrors", 120, 1000)],
        "hints": {
            "power_kw": "1.5",
            "lifespan_hours": "10000",
            "failure_pct": "3",
            "maintenance_interval_hours": "200",
        },
    },
    "printer_3d": {
        "wear_parts": [("Nozzle / hotend", 15, 500), ("Build plate", 60, 2000)],
        "hints": {
            "power_kw": "0.15",
            "lifespan_hours": "5000",
            "failure_pct": "10",
            "maintenance_interval_hours": "500",
        },
    },
    "cnc": {
        "wear_parts": [("Router bits", 60, 40), ("Spindle rebuild", 400, 5000)],
        "hints": {
            "power_kw": "3",
            "lifespan_hours": "20000",
            "failure_pct": "3",
            "maintenance_interval_hours": "250",
        },
    },
    "plasma": {
        "wear_parts": [("Consumables (electrode and nozzle)", 30, 2), ("Torch", 300, 1000)],
        "hints": {
            "power_kw": "8",
            "lifespan_hours": "10000",
            "consumables_per_hour": "3",
            "failure_pct": "3",
        },
    },
    "lathe": {
        "wear_parts": [("Tooling inserts", 40, 50)],
        "hints": {"power_kw": "2", "lifespan_hours": "30000"},
    },
    "saw": {
        "wear_parts": [("Blades", 80, 100)],
        "hints": {"power_kw": "2", "lifespan_hours": "20000"},
    },
    "welder": {
        "wear_parts": [("Contact tips and liners", 20, 20)],
        "hints": {"power_kw": "5", "lifespan_hours": "15000", "consumables_per_hour": "6"},
    },
    "other": {"wear_parts": [], "hints": {}},
}
