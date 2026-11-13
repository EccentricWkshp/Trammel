"""HTTPS to the public internet only, for addresses someone typed in (a tax
file's URL, an online store's address): never into this machine or its network.

Each hop is resolved and every address it resolves to must be public; the
connection then goes to the address checked, with the certificate still checked
against the host name, so the name can't be pointed elsewhere in between (DNS
rebinding). Redirects are followed only to an https address that passes the same
check; one that carries credentials can be held to its own origin (SEC-10,
0.10.6; first written for the tax downloader).
"""

from __future__ import annotations

import functools
import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

from django.core.exceptions import ValidationError

MAX_REDIRECTS = 5
REDIRECTS = (301, 302, 303, 307, 308)
#: Headers that never go on to another origin.
CREDENTIALS = ("Authorization", "Cookie", "X-shopify-access-token", "X-api-key")


def public_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a host name written as a number stands for (127.0.0.1,
    2130706433, 0x7f.1), or None for a name to resolve."""
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        try:
            # inet_aton reads the old short and numeric forms resolvers accept.
            address = ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
        except OSError:
            return None
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


def check_url(url: str) -> str:
    """Refuse anything but https to a public address. Returns the address
    checked, which the request then connects to."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValidationError("Give an https:// address.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except OSError as exc:
        raise ValidationError(f"Cannot resolve {parsed.hostname}.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            raise ValidationError("That address is on a private network.")
    return infos[0][4][0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to one checked address, with the certificate still checked against
    the host name."""

    def __init__(self, host, *args, address: str, **kwargs):
        super().__init__(host, *args, **kwargs)
        self._address = address

    def connect(self):
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, address: str):
        super().__init__(context=ssl.create_default_context())
        self._address = address

    def https_open(self, req):
        return self.do_open(
            functools.partial(PinnedHTTPSConnection, address=self._address),
            req,
            context=self._context,
        )


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Redirects come back as errors, so each new address is checked first."""

    def redirect_request(self, *args, **kwargs):
        return None


def _origin(url: str) -> tuple:
    parsed = urlparse(url)
    return parsed.scheme, (parsed.hostname or "").lower(), parsed.port or 443


def open_url(request: urllib.request.Request, *, timeout: float, same_origin: bool = False):
    """Open `request` (https to a public address), following redirects only to
    https addresses that pass the same check. With `same_origin`, a redirect to
    another origin is refused; otherwise credentials are dropped on the way
    there. Only a GET or HEAD is redirected. Raises ValidationError for an
    address refused, urllib's errors otherwise."""
    start = request.full_url
    for _ in range(MAX_REDIRECTS + 1):
        address = check_url(request.full_url)
        opener = urllib.request.build_opener(NoRedirects, PinnedHTTPSHandler(address))
        try:
            return opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.code in REDIRECTS else None
            if not location:
                raise
            target = urljoin(request.full_url, location)
            if urlparse(target).scheme != "https":
                raise ValidationError("The address redirects to one that isn't https.") from None
            if request.get_method() not in ("GET", "HEAD"):
                raise ValidationError("The address redirected a request it can't follow.") from None
            headers = dict(request.header_items())
            if _origin(target) != _origin(start):
                if same_origin:
                    raise ValidationError("The address redirects to another site.") from None
                headers = {k: v for k, v in headers.items() if k.capitalize() not in CREDENTIALS}
            request = urllib.request.Request(  # noqa: S310 — https, checked above
                target, headers=headers, method=request.get_method()
            )
    raise ValidationError("The address redirects too many times.")
