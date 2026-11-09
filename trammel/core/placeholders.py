"""What each kind of user template can use, for the editor's preview and reference.

Modules register a `Kind` for each template they render (quotes, invoices,
agreements, each email). A kind lists its placeholders in groups and knows how
to build a preview context: from the most recent real record when there is
one, otherwise from sample data, so the preview and the examples always show
something.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from . import templating


@dataclass(frozen=True)
class P:
    """One placeholder: what to write, and what it means."""

    expr: str
    meaning: str


@dataclass(frozen=True)
class Group:
    title: str
    items: tuple[P, ...]
    #: For a list: the loop that walks it, e.g. ("line", "quote.lines").
    loop: tuple[str, str] | None = None


@dataclass
class Kind:
    key: str
    title: str
    groups: tuple[Group, ...]
    #: Returns (context, where it came from), e.g. "quote Q-2026-0003, revision 2".
    preview: Callable[[], tuple[dict, str]]
    html: bool = True
    module: str = ""
    #: Who may preview it: an action permission, or "" for system admins only.
    permission: str = ""
    #: True for an email: the editor has a subject as well as a body.
    has_subject: bool = False
    #: Shown above the reference, e.g. who receives an email.
    note: str = ""
    order: int = 100
    extra: dict = field(default_factory=dict)


_kinds: dict[str, Kind] = {}


def register(kind: Kind) -> Kind:
    _kinds[kind.key] = kind
    return kind


def get(key: str) -> Kind | None:
    return _kinds.get(key)


def kinds() -> list[Kind]:
    """Registered kinds whose module is switched on, in order."""
    from . import modules

    enabled = modules.enabled_keys()
    return sorted(
        (k for k in _kinds.values() if not k.module or k.module in enabled),
        key=lambda k: (k.order, k.title),
    )


# ---------------------------------------------------------------------------
# Shared groups
# ---------------------------------------------------------------------------

BUSINESS = Group(
    "Your business",
    (
        P("business.name", "Business name"),
        P("business.legal_name", "Legal name (the business name if not set)"),
        P("business.logo", "Logo image, for an <img> tag's src. Blank when there is none"),
        P("business.phone", "Phone"),
        P("business.email", "Email"),
        P("business.website", "Website"),
        P("business.address_lines", "Address, as a list of lines"),
        P("business.tax_registration_numbers", "Tax registration numbers, as a list"),
        P("business.currency", "Currency code, e.g. USD"),
    ),
)

CUSTOMER = Group(
    "Customer",
    (
        P("customer.name", "Name"),
        P("customer.company", "Company / organization"),
        P("customer.email", "Email"),
        P("customer.phone", "Phone"),
        P("customer.address_lines", "Billing address, as a list of lines"),
        P("customer.tax_exempt", "Tax exempt (true or false)"),
        P("customer.tax_exempt_number", "Exemption certificate / reseller permit"),
    ),
)

FILTERS = (
    ("{{ quote.total|money }}", "An amount as currency", "$1,234.50"),
    ("{{ invoice.due_date|date }}", "A date written out", "November 8, 2026"),
    ('{{ invoice.due_date|date("%m/%d/%Y") }}', "A date in your own format", "11/08/2026"),
    ("{{ line.discount_pct|percent }}", "A percentage", "12.5%"),
    ("{{ customer.name|upper }}", "Capital letters (also lower, title)", "GRACE HOLLIS"),
    ("{{ quote.notes|default('None') }}", "Something to show when a value is blank", "None"),
)

LOGIC = (
    ("{% if customer.company %}…{% endif %}", "Show a part only when a value is filled in"),
    ("{% if invoice.balance > 0 %}…{% else %}…{% endif %}", "Choose between two parts"),
    ("{% for line in quote.lines %}…{% endfor %}", "Repeat a part for each item in a list"),
    ("{% for line in business.address_lines %}{{ line }}<br>{% endfor %}", "Print a list"),
    (
        "{% if business.logo %}"
        '<img src="{{ business.logo }}" alt="{{ business.name }}">'
        "{% endif %}",
        "Show the logo",
    ),
)


# ---------------------------------------------------------------------------
# Sample data, for previews before any real record exists
# ---------------------------------------------------------------------------

SAMPLE_CUSTOMER = {
    "name": "Grace Hollis",
    "company": "Hollis Design Studio",
    "email": "grace@example.com",
    "phone": "(253) 555-0147",
    "address_lines": ["1402 Pacific Avenue", "Tacoma, WA 98402"],
    "tax_exempt": False,
    "tax_exempt_number": "",
}

SAMPLE_JOB = {
    "number": "J-2026-0006",
    "title": "Walnut dining table",
    "description": "72 × 38 in walnut dining table with breadboard ends, Danish oil finish.",
    "due_date": dt.date(2026, 12, 4),
    "promised_date": dt.date(2026, 12, 4),
}


def sample_lines() -> list[dict]:
    return [
        {
            "description": "Black walnut 4/4, select",
            "quantity": Decimal("38"),
            "unit": "board ft",
            "unit_price": Decimal("18.50"),
            "discount_pct": Decimal("0"),
            "total": Decimal("703.00"),
            "taxable": True,
        },
        {
            "description": "Milling, glue-up, joinery and finish",
            "quantity": Decimal("22"),
            "unit": "hour",
            "unit_price": Decimal("90.00"),
            "discount_pct": Decimal("0"),
            "total": Decimal("1980.00"),
            "taxable": True,
        },
    ]


def business() -> dict:
    from .models import BusinessProfile

    return templating.business_context(BusinessProfile.load())


# ---------------------------------------------------------------------------
# Examples for the reference
# ---------------------------------------------------------------------------


def _example(expr: str, ctx: dict) -> str:
    from .formats import show
    from .money import trim_decimal

    try:
        value = templating.environment(html=False).compile_expression(expr)(**ctx)
    except Exception:  # noqa: BLE001 — an example is a nicety; never fail the page
        return ""
    if value is None or value == "" or value == []:
        return "(blank)"
    if isinstance(value, dt.datetime | dt.date):
        text = show(value, "dt" if isinstance(value, dt.datetime) else "d")
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, Decimal):
        text = trim_decimal(value)
    elif isinstance(value, list | tuple):
        text = " / ".join(str(v) for v in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= 70 else text[:67] + "…"


def reference(kind: Kind, ctx: dict | None = None) -> list[dict]:
    """The kind's groups with an example value for each placeholder."""
    out = []
    for group in kind.groups:
        local = dict(ctx or {})
        if group.loop and ctx is not None:
            var, source = group.loop
            items = _lookup(ctx, source)
            local[var] = items[0] if items else None
        out.append(
            {
                "title": group.title,
                "loop": group.loop,
                "items": [
                    {"expr": p.expr, "meaning": p.meaning, "example": _example(p.expr, local)}
                    for p in group.items
                ],
            }
        )
    return out


def _lookup(ctx: dict, path: str):
    value = ctx
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value if isinstance(value, list) else []


def allowed(actor, kind: Kind) -> bool:
    from . import access

    if kind.permission:
        return access.has_perm(actor, kind.permission)
    return access.is_admin(actor)


def editor_context(key: str) -> dict:
    """Context for `core/template_editor.html`: preview target and the reference."""
    from django.urls import reverse

    kind = _kinds[key]
    try:
        ctx, source = kind.preview()
    except Exception:  # noqa: BLE001 — a broken record must not break the editor
        ctx, source = None, ""
    return {
        "kind": kind,
        "reference": reference(kind, ctx),
        "preview_source": source,
        "preview_url": reverse("core:template_preview", args=[key]),
        "filters": FILTERS,
        "logic": LOGIC,
    }
