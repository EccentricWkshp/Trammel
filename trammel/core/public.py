"""Where Trammel can be reached from the internet, when something stands in for
it: the edge portal on Cloudflare (0.9.x) takes the portal and the online
stores' webhooks and callbacks at Trammel's own paths, at its own address.
Modules that show or hand out a public address ask here; the edge portal
registers itself."""

from __future__ import annotations

from collections.abc import Callable

from django.urls import reverse

#: Each returns the public base address ("https://portal.example.com"), or "".
providers: list[Callable[[], str]] = []


def base() -> str:
    for fn in providers:
        if url := fn():
            return url.rstrip("/")
    return ""


def url(request, name: str, *args) -> str:
    """The public address of a page: on the stand-in when there is one, else
    this server's own."""
    path = reverse(name, args=args)
    stand_in = base()
    return stand_in + path if stand_in else request.build_absolute_uri(path)
