"""
User-authored templates: quotes, invoices, agreements, emails (FR-QUO-5, FR-BIL-3,
FR-AGR-1, FR-NTF-3).

These are written by users and therefore untrusted. They run in Jinja's
`SandboxedEnvironment`, and they are given plain dicts and lists built for the
purpose, never model instances: a model would let a template walk relations to
other customers' records even inside the sandbox.

The same HTML is shown on screen and rendered to PDF (SPEC §4.1).
"""

from __future__ import annotations

import datetime as dt
import functools
import warnings
from decimal import Decimal

from jinja2 import TemplateSyntaxError, Undefined
from jinja2.exceptions import SecurityError, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from .money import format_money


class TemplateProblem(ValueError):
    """A user template that does not parse or does not render."""


class PdfUnavailable(RuntimeError):
    """WeasyPrint or its system libraries are missing on this host."""


def _money(value, currency=None):
    if value in (None, ""):
        return ""
    return format_money(Decimal(str(value)), currency)


def _date(value, fmt="%B %-d, %Y"):
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = dt.date.fromisoformat(value[:10])
        except ValueError:
            return value
    # %-d is not portable to Windows; strip a leading zero by hand instead.
    return value.strftime(fmt.replace("%-d", "%d")).replace(" 0", " ")


def _percent(value):
    if value in (None, ""):
        return ""
    return f"{Decimal(str(value)).normalize():f}%"


#: The most a template may make by repeating text or a list, and the largest
#: number it may make: plenty for any document, too little to stall the server.
MAX_REPEAT = 100_000
MAX_NUMBER = 10**24


def _too_big(operator: str, left, right) -> str:
    """Why `left operator right` would be too large to work out, or ""."""
    numbers = (int, float, Decimal)
    if operator == "**" and isinstance(left, numbers) and isinstance(right, numbers):
        digits = len(str(int(abs(left)))) if abs(left) > 1 else 0
        if abs(right) > 64 or abs(right) * digits > 24:
            return "A power in this template is too large."
    if operator == "*":
        for value, times in ((left, right), (right, left)):
            repeated = isinstance(value, (str, list, tuple)) and isinstance(times, int)
            if repeated and len(value) * times > MAX_REPEAT:
                return "Text repeated in this template is too long."
    return ""


class _Sandbox(SandboxedEnvironment):
    """The sandbox, with `*` and `**` checked: a template can't make a huge
    number or text that ties up the server (`'a' * 10**9`, `9 ** 9 ** 9`)."""

    intercepted_binops = frozenset(["*", "**"])

    def call_binop(self, context, operator, left, right):
        if problem := _too_big(operator, left, right):
            raise TemplateProblem(problem)
        result = super().call_binop(context, operator, left, right)
        if isinstance(result, int) and abs(result) > MAX_NUMBER:
            raise TemplateProblem("A number in this template is too large.")
        return result


def environment(*, html: bool = True) -> SandboxedEnvironment:
    env = _Sandbox(autoescape=html, undefined=Undefined, trim_blocks=True, lstrip_blocks=True)
    env.filters["money"] = _money
    env.filters["date"] = _date
    env.filters["percent"] = _percent
    return env


def validate(source: str, *, html: bool = True) -> list[str]:
    try:
        environment(html=html).parse(source)
    except TemplateSyntaxError as exc:
        return [f"Line {exc.lineno}: {exc.message}"]
    return []


def render(source: str, context: dict, *, html: bool = True) -> str:
    try:
        return environment(html=html).from_string(source).render(**context)
    except TemplateSyntaxError as exc:
        raise TemplateProblem(f"Line {exc.lineno}: {exc.message}") from exc
    except SecurityError as exc:
        raise TemplateProblem(f"Not allowed in a template: {exc}") from exc
    except TemplateError as exc:
        raise TemplateProblem(str(exc)) from exc


def _import_weasyprint():
    # WeasyPrint warns at import about optional system libraries it will need in
    # a later release; that notice says nothing about whether PDFs work now.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import weasyprint

    return weasyprint


@functools.cache
def pdf_available() -> bool:
    try:
        _import_weasyprint()
    except ImportError, OSError:
        return False
    return True


def render_pdf(html: str) -> bytes:
    """Render a full HTML document to PDF. External URLs are never fetched."""
    try:
        weasyprint = _import_weasyprint()
    except (ImportError, OSError) as exc:
        raise PdfUnavailable(
            "This server cannot make PDF files: WeasyPrint's system libraries are missing "
            "(the Docker image includes them). Use Print and choose Save as PDF instead."
        ) from exc

    # Only data: URLs (the embedded logo) are read; anything else — files on the
    # server, addresses on the network — is refused.
    fetcher = weasyprint.URLFetcher(allowed_protocols={"data"}, allow_redirects=False)
    return weasyprint.HTML(string=html, url_fetcher=fetcher).write_pdf()


def business_context(business) -> dict:
    """The business as a template sees it.

    Details the business has chosen not to show on documents arrive empty, so
    every template, including ones written before the choice was made, leaves
    them out without needing a condition of its own.
    """
    from .logo import data_uri

    address = [
        business.address_line1,
        business.address_line2,
        " ".join(
            p
            for p in [
                business.city + ("," if business.region else ""),
                business.region,
                business.postal_code,
            ]
            if p
        ),
    ]
    return {
        "name": business.name,
        "legal_name": business.legal_name or business.name,
        "logo": data_uri(business) if business.show_logo else "",
        "phone": business.phone if business.show_phone else "",
        "email": business.email if business.show_email else "",
        "website": business.website if business.show_website else "",
        "address_lines": (
            [line for line in address if line.strip()] if business.show_address else []
        ),
        "tax_registration_numbers": (
            [line for line in business.tax_registration_numbers.splitlines() if line.strip()]
            if business.show_tax_registration_numbers
            else []
        ),
        "currency": business.currency,
    }


def customer_context(customer) -> dict:
    billing = customer.default_address("billing")
    return {
        "name": customer.name,
        "company": customer.company,
        "email": customer.email,
        "phone": customer.phone,
        "address_lines": billing.lines() if billing else [],
        "tax_exempt": customer.tax_exempt,
        "tax_exempt_number": customer.tax_exempt_number,
    }
