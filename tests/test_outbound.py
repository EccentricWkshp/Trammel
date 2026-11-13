"""Addresses someone typed in reach only the public internet, and redirects are
checked hop by hop (SEC-10, 0.10.6)."""

import io
import urllib.error
import urllib.request

import pytest
from django.core.exceptions import ValidationError

from trammel.core import outbound
from trammel.imports import woocommerce


def test_an_address_written_as_a_number_is_checked():
    assert str(outbound.public_address("2130706433")) == "127.0.0.1"
    assert str(outbound.public_address("0x7f.1")) == "127.0.0.1"
    assert str(outbound.public_address("[::ffff:10.0.0.1]")) == "10.0.0.1"
    assert outbound.public_address("shop.example.com") is None
    for bad in (
        "https://2130706433",
        "https://0x7f.1",
        "https://10.1.2.3",
        "https://169.254.169.254",
        "https://shop.localhost",
    ):
        with pytest.raises(ValidationError):
            woocommerce.check_url(bad)
    assert woocommerce.check_url("https://203.0.113.0.example.com") == (
        "https://203.0.113.0.example.com"
    )


def test_a_name_resolving_to_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(
        outbound.socket,
        "getaddrinfo",
        lambda host, port, *rest: [(2, 1, 6, "", ("10.0.0.5", port))],
    )
    with pytest.raises(ValidationError, match="private"):
        outbound.check_url("https://store.example.com/wp-json")


class _Redirecting:
    """An opener whose first answer is a redirect to `location`."""

    def __init__(self, location, seen):
        self.location, self.seen = location, seen

    def open(self, request, timeout):
        self.seen.append((request.full_url, dict(request.header_items())))
        if len(self.seen) == 1:
            headers = {"Location": self.location}
            raise urllib.error.HTTPError(request.full_url, 302, "Found", headers, io.BytesIO())
        return "opened"


def _redirect_to(monkeypatch, location):
    seen = []
    monkeypatch.setattr(outbound, "check_url", lambda url: "203.0.113.7")
    monkeypatch.setattr(
        outbound.urllib.request, "build_opener", lambda *handlers: _Redirecting(location, seen)
    )
    return seen


def _request():
    return urllib.request.Request(
        "https://store.example.com/orders", headers={"Authorization": "Basic c2VjcmV0"}
    )


def test_redirects_never_drop_to_http(monkeypatch):
    _redirect_to(monkeypatch, "http://127.0.0.1:8000/private")
    with pytest.raises(ValidationError, match="isn't https"):
        outbound.open_url(_request(), timeout=5)


def test_credentials_stay_with_their_site(monkeypatch):
    _redirect_to(monkeypatch, "https://elsewhere.example.net/orders")
    with pytest.raises(ValidationError, match="another site"):
        outbound.open_url(_request(), timeout=5, same_origin=True)
    seen = _redirect_to(monkeypatch, "https://elsewhere.example.net/orders")
    assert outbound.open_url(_request(), timeout=5) == "opened"
    assert seen[1][0] == "https://elsewhere.example.net/orders"
    assert "Authorization" not in seen[1][1]
    # On the same site, they go along.
    seen = _redirect_to(monkeypatch, "/orders?page=2")
    assert outbound.open_url(_request(), timeout=5, same_origin=True) == "opened"
    assert seen[1][1]["Authorization"] == "Basic c2VjcmV0"
