"""Display formats for dates and times, chosen per business (US by default).

Set once per request by the timezone middleware; template filters `d`, `dt`,
`t`, `md` and `mdt` read it. Form inputs always use ISO dates — that is what
HTML date fields require — so only display goes through here.
"""

from __future__ import annotations

from contextvars import ContextVar

FORMATS = {
    "us": {"d": "m/d/Y", "dt": "m/d/Y g:i A", "t": "g:i A", "md": "n/j", "mdt": "n/j g:i A"},
    "iso": {"d": "Y-m-d", "dt": "Y-m-d H:i", "t": "H:i", "md": "m-d", "mdt": "m-d H:i"},
    "dmy": {"d": "d/m/Y", "dt": "d/m/Y H:i", "t": "H:i", "md": "j/n", "mdt": "j/n H:i"},
}
CHOICES = [
    ("us", "11/07/2026 and 2:30 PM (US)"),
    ("iso", "2026-11-07 and 14:30 (ISO)"),
    ("dmy", "07/11/2026 and 14:30 (day first)"),
]

_current: ContextVar[str] = ContextVar("date_format", default="us")


def activate(style: str):
    return _current.set(style if style in FORMATS else "us")


def deactivate(token) -> None:
    _current.reset(token)


def pattern(kind: str) -> str:
    return FORMATS[_current.get()][kind]


def show(value, kind: str = "d") -> str:
    """Format a date (or datetime) in Python code the way templates do."""
    from django.utils import dateformat
    from django.utils.timezone import template_localtime

    return dateformat.format(template_localtime(value), pattern(kind)) if value else ""
