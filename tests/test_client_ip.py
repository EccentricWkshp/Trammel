"""The client address behind zero, one or more reverse proxies."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, override_settings

from trammel.core.middleware import client_ip


def _request(remote="203.0.113.5", forwarded=None):
    meta = {"REMOTE_ADDR": remote}
    if forwarded is not None:
        meta["HTTP_X_FORWARDED_FOR"] = forwarded
    return RequestFactory().get("/", **meta)


@override_settings(TRUSTED_PROXIES=[])
def test_no_trusted_proxies_ignores_the_header():
    assert client_ip(_request(forwarded="198.51.100.7")) == "203.0.113.5"


@override_settings(TRUSTED_PROXIES=["*"])
def test_built_in_proxy_is_believed():
    # Caddy replaces whatever the client sent, so the header holds one address.
    assert client_ip(_request(remote="172.18.0.4", forwarded="198.51.100.7")) == "198.51.100.7"


@override_settings(TRUSTED_PROXIES=["*"])
def test_no_header_falls_back_to_the_connection():
    assert client_ip(_request(remote="172.18.0.4")) == "172.18.0.4"


@override_settings(TRUSTED_PROXIES=["192.168.1.10"])
def test_external_proxy_is_believed():
    request = _request(remote="192.168.1.10", forwarded="192.168.1.77")
    assert client_ip(request) == "192.168.1.77"


@override_settings(TRUSTED_PROXIES=["192.168.1.10"])
def test_direct_connection_cannot_forge_an_address():
    # Someone on the LAN connecting to the published port directly.
    request = _request(remote="192.168.1.66", forwarded="10.9.9.9")
    assert client_ip(request) == "192.168.1.66"


@override_settings(TRUSTED_PROXIES=["192.168.1.10"])
def test_forged_hops_left_of_the_proxy_are_ignored():
    # The client sent its own X-Forwarded-For; the proxy appended the real one.
    request = _request(remote="192.168.1.10", forwarded="10.9.9.9, 192.168.1.77")
    assert client_ip(request) == "192.168.1.77"


@override_settings(TRUSTED_PROXIES=["10.0.0.0/8", "192.168.1.10"])
def test_chained_proxies_are_skipped():
    request = _request(remote="192.168.1.10", forwarded="192.168.1.77, 10.0.0.3")
    assert client_ip(request) == "192.168.1.77"


@override_settings(TRUSTED_PROXIES=["192.168.1.10"])
def test_garbage_hop_is_neither_taken_nor_skipped_past():
    request = _request(remote="192.168.1.10", forwarded="10.9.9.9, not-an-ip")
    assert client_ip(request) == "192.168.1.10"


@override_settings(TRUSTED_PROXIES=["*"])
def test_with_any_proxy_trusted_only_the_hop_it_appended_is_believed():
    # A client that sends its own X-Forwarded-For through a proxy that appends
    # to it can't choose its address.
    request = _request(remote="172.18.0.4", forwarded="10.9.9.9, 198.51.100.7")
    assert client_ip(request) == "198.51.100.7"


def test_bad_setting_is_refused(monkeypatch):
    import importlib

    import config.settings as settings_module

    monkeypatch.setenv("TRUSTED_PROXIES", "192.168.1.10,proxy.lan")
    with pytest.raises(ImproperlyConfigured, match="proxy.lan"):
        importlib.reload(settings_module)
    monkeypatch.delenv("TRUSTED_PROXIES")
    importlib.reload(settings_module)
