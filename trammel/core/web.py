"""Helpers shared by every module's views."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse, StreamingHttpResponse
from django.middleware.csp import get_nonce
from django.utils.csp import CSP
from django.utils.http import content_disposition_header
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.csp import csp_override

from . import access

#: Printable documents (quotes, invoices, travelers) carry their own inline styles and
#: may come from a user template, so they get their own policy: inline styles, data:
#: images, no network, sandboxed, framed only by this site. The one script allowed is
#: the print call `document_response` adds, which carries this request's nonce.
DOCUMENT_CSP = {
    "default-src": [CSP.NONE],
    "style-src": [CSP.UNSAFE_INLINE],
    "img-src": ["data:"],
    "script-src": [CSP.NONCE],
    "frame-ancestors": [CSP.SELF],
    "sandbox": ["allow-scripts", "allow-modals"],
}


def document_view(view):
    """Serve a view's printable document under DOCUMENT_CSP."""
    return xframe_options_sameorigin(csp_override(DOCUMENT_CSP)(view))


def document_response(request, html: str) -> HttpResponse:
    """The document as HTML; with ?print=1 it opens the browser's print dialog on load."""
    if request.GET.get("print"):
        script = (
            f'<script nonce="{get_nonce(request)}">'
            'addEventListener("load", function () { print(); });</script>'
        )
        if "</body>" in html:
            html = html.replace("</body>", script + "</body>", 1)
        else:
            html += script
    return HttpResponse(html)


def permission_required(codename: str):
    """Sign-in plus an action permission; 403 without it."""

    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            access.require(request.actor, codename)
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def admin_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        access.require_admin(request.actor)
        return view(request, *args, **kwargs)

    return wrapped


def paginate(request, queryset, per_page: int = 50):
    return Paginator(queryset, per_page).get_page(request.GET.get("page"))


class _Echo:
    def write(self, value):
        return value


def csv_response(
    filename: str, header: list[str], rows: Iterable[Iterable]
) -> StreamingHttpResponse:
    """Stream a CSV export (FR-BAK-6, FR-RPT-7)."""
    writer = csv.writer(_Echo())

    def generate():
        yield "﻿"  # BOM, so spreadsheet software detects UTF-8
        yield writer.writerow(header)
        for row in rows:
            yield writer.writerow([_cell(v) for v in row])

    response = StreamingHttpResponse(generate(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = content_disposition_header(True, f"{filename}")
    return response


def _cell(value):
    """Neutralize spreadsheet formula injection in exported text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return "" if value is None else value


def safe_next(request, fallback: str) -> str:
    """The `next` address a form sent, if it stays on this site; else `fallback`."""
    from django.utils.http import url_has_allowed_host_and_scheme

    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return target
    return fallback
