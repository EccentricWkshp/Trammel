from django import template

from trammel.core.money import format_money

register = template.Library()


@register.filter
def startswith(value: str, prefix: str) -> bool:
    return bool(prefix) and str(value).startswith(str(prefix))


def _fmt(value, kind):
    from django.template.defaultfilters import date as date_filter
    from django.utils.timezone import template_localtime

    from trammel.core.formats import pattern

    # Called from Python, the date filter skips the template engine's conversion
    # of aware datetimes to the active time zone, so convert here.
    return date_filter(template_localtime(value), pattern(kind)) if value else ""


@register.filter
def d(value):
    """A date in the business's format (US: 11/07/2026)."""
    return _fmt(value, "d")


@register.filter
def dt(value):
    """A date and time (US: 11/07/2026 2:30 PM), in the viewer's time zone."""
    return _fmt(value, "dt")


@register.filter
def t(value):
    """A time of day (US: 2:30 PM)."""
    return _fmt(value, "t")


@register.filter
def md(value):
    """Month and day only (US: 11/7)."""
    return _fmt(value, "md")


@register.filter
def mdt(value):
    """Month, day and time (US: 11/7 2:30 PM)."""
    return _fmt(value, "mdt")


@register.filter
def money(amount):
    """Format an amount in the instance currency."""
    return format_money(amount)


@register.filter
def qty(value):
    """A quantity without padding zeros, grouped by thousands: 1200.5000 -> 1,200.5."""
    if value in (None, ""):
        return ""
    from decimal import Decimal

    from trammel.core.money import trim_decimal

    text = trim_decimal(Decimal(value))
    whole, _, frac = text.partition(".")
    sign = "-" if whole.startswith("-") else ""
    whole = f"{int(whole.lstrip('-') or 0):,}"
    return f"{sign}{whole}.{frac}" if frac else f"{sign}{whole}"


@register.filter
def minutes(value):
    """A time in minutes for reading: 0 -> —, 45 -> 45 min, 90 -> 1 h 30 min, 7.5 -> 7.5 min."""
    if value in (None, ""):
        return ""
    from decimal import Decimal

    from trammel.core.money import trim_decimal

    total = Decimal(value)
    if not total:
        return "—"
    hours, rest = divmod(total, 60)
    rest_text = f"{trim_decimal(rest.quantize(Decimal('0.1')))} min" if rest else ""
    if not hours:
        return rest_text
    return f"{int(hours)} h {rest_text}".strip()


@register.filter
def signature_uri(value):
    """A stored signature PNG as a data: URL, for an <img>."""
    from trammel.core.signature import data_uri

    return data_uri(value) if value else ""


@register.filter
def hours(value):
    """A time in hours for reading: 0 -> —, 1 -> 1 hour, 12 -> 12 hours, 1.5 -> 1.5 hours."""
    if value in (None, ""):
        return ""
    from decimal import Decimal

    from trammel.core.money import trim_decimal

    total = Decimal(value)
    if not total:
        return "—"
    text = trim_decimal(total.quantize(Decimal("0.01")))
    return f"{text} hour{'' if total == 1 else 's'}"


@register.filter
def pct(value):
    """A percentage without trailing zeros: 6.5000 -> 6.5%, 10.0000 -> 10%."""
    if value in (None, ""):
        return ""
    from trammel.tax.services import rate_text

    return rate_text(value)


# ---------------------------------------------------------------------------
# Sorting list pages (core/sorting.py)
# ---------------------------------------------------------------------------


@register.simple_tag(takes_context=True)
def sort_th(context, key: str, label: str, css: str = ""):
    """A column heading that sorts the list by its column; a second click reverses."""
    from django.utils.html import format_html
    from django.utils.safestring import mark_safe

    link = (context.get("sorting") or {}).get("links", {}).get(key)
    classes = format_html(' class="{}"', css) if css else mark_safe("")
    if link is None:
        return format_html('<th scope="col"{}>{}</th>', classes, label)
    aria = format_html(' aria-sort="{}"', link["aria"]) if link["aria"] else mark_safe("")
    arrow = (
        format_html(' <span aria-hidden="true">{}</span>', link["arrow"])
        if link["arrow"]
        else mark_safe("")
    )
    return format_html(
        '<th scope="col"{}{}><a class="sort-link" href="{}">{}{}</a></th>',
        classes,
        aria,
        link["url"],
        label,
        arrow,
    )


@register.inclusion_tag("partials/sort_select.html", takes_context=True)
def sort_select(context):
    """The list's sorts as a menu, for its filter form."""
    return {"sorting": context.get("sorting")}
