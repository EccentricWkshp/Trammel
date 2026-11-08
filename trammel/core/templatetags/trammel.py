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
def pct(value):
    """A percentage without trailing zeros: 6.5000 -> 6.5%, 10.0000 -> 10%."""
    if value in (None, ""):
        return ""
    from trammel.tax.services import rate_text

    return rate_text(value)
