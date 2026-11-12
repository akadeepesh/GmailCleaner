"""
Tests for gmail/url_guard.py

Covers:
  - is_public_ip: private, loopback, link-local, CGNAT, unspecified, IPv6
    local ranges, IPv4-mapped / NAT64 forms, multicast, garbage
  - resolve_public: obscure IP spellings, internal names, mixed public/private
    DNS answers, unresolvable hosts (socket.getaddrinfo is always mocked)
  - check_url: scheme, userinfo, port rules
  - GuardedBackend / GuardedTransport: connect to the vetted IP, never the
    hostname; a private answer blocks before any connection is made
"""

import socket
from unittest.mock import MagicMock, patch

import httpcore
import httpx
import pytest

from gmail.url_guard import (
    BlockedDestination,
    GuardedBackend,
    GuardedTransport,
    check_url,
    is_public_ip,
    resolve_public,
)


def _addrinfo(*ips: str):
    """Fake socket.getaddrinfo results for the given IPs."""
    out = []
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
        out.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
    return out


@pytest.fixture
def dns():
    """Patch getaddrinfo; tests set dns.return_value. Fails loudly if unset."""
    with patch("gmail.url_guard.socket.getaddrinfo") as mock:
        mock.side_effect = AssertionError("unexpected DNS lookup")
        yield mock


def _answer(dns, *ips):
    dns.side_effect = None
    dns.return_value = _addrinfo(*ips)


# ── is_public_ip ──────────────────────────────────────────────────────────────

BLOCKED_IPS = [
    "127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "fc00::1",
    "::ffff:127.0.0.1", "::ffff:169.254.169.254",
    "224.0.1.1", "ff02::1",                # multicast
    "64:ff9b::7f00:1",                     # NAT64 of 127.0.0.1
    "2002:7f00:1::",                       # 6to4 of 127.0.0.1
    "fe80::1%eth0",                        # zone id
    "not-an-ip", "", "999.1.1.1",
]


class TestIsPublicIp:
    @pytest.mark.parametrize("ip", BLOCKED_IPS)
    def test_blocked(self, ip):
        assert is_public_ip(ip) is False

    @pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700::1111",
                                    "::ffff:8.8.8.8"])
    def test_public(self, ip):
        assert is_public_ip(ip) is True


# ── resolve_public ────────────────────────────────────────────────────────────

class TestResolvePublic:
    @pytest.mark.parametrize("ip", BLOCKED_IPS[:12])
    def test_blocked_literals_without_dns(self, dns, ip):
        with pytest.raises(BlockedDestination):
            resolve_public(ip, 80)
        dns.assert_not_called()

    @pytest.mark.parametrize("url", ["http://2130706433/", "http://0x7f.0.0.1/",
                                     "http://127.1/"])
    def test_obscure_ip_spellings_blocked_when_resolved(self, dns, url):
        # Even if a resolver turns these into 127.0.0.1, the result is judged.
        _answer(dns, "127.0.0.1")
        host = httpx.URL(url).host
        with pytest.raises(BlockedDestination):
            resolve_public(host, 80)

    @pytest.mark.parametrize("url", ["http://2130706433/", "http://0x7f.0.0.1/",
                                     "http://127.1/"])
    def test_obscure_ip_spellings_blocked_by_check_url(self, dns, url):
        with pytest.raises(BlockedDestination):
            check_url(url)
        dns.assert_not_called()

    @pytest.mark.parametrize("host", ["localhost", "metadata.google.internal",
                                      "foo.local", "intranet", "LOCALHOST.",
                                      "app.localhost", "nas.home.arpa", "box.lan"])
    def test_internal_names_blocked_without_dns(self, dns, host):
        with pytest.raises(BlockedDestination):
            resolve_public(host, 80)
        dns.assert_not_called()

    def test_mixed_public_and_private_answer_blocked(self, dns):
        _answer(dns, "93.184.216.34", "10.0.0.5")
        with pytest.raises(BlockedDestination, match="non-public"):
            resolve_public("unsub.example.com", 443)

    def test_mapped_private_answer_blocked(self, dns):
        _answer(dns, "::ffff:169.254.169.254")
        with pytest.raises(BlockedDestination):
            resolve_public("unsub.example.com", 443)

    def test_public_answer_returned(self, dns):
        _answer(dns, "93.184.216.34", "2606:2800:220:1::1", "93.184.216.34")
        assert resolve_public("unsub.example.com", 443) == ["93.184.216.34", "2606:2800:220:1::1"]

    def test_unresolvable_blocked(self, dns):
        dns.side_effect = socket.gaierror("nope")
        with pytest.raises(BlockedDestination, match="does not resolve"):
            resolve_public("nx.example.com", 443)

    def test_empty_answer_blocked(self, dns):
        _answer(dns)
        with pytest.raises(BlockedDestination):
            resolve_public("empty.example.com", 443)


# ── check_url ─────────────────────────────────────────────────────────────────

class TestCheckUrl:
    @pytest.mark.parametrize("url", [
        "http://user:pass@example.com/",
        "http://user@example.com/",
        "http://example.com:22/",
        "https://example.com:6379/",
        "http://example.com:99999/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http:///nohost",
        "http://169.254.169.254/computeMetadata/v1/",
        "http://127.0.0.1:8080/api/execute",
        "http://[::1]/",
        "http://localhost/",
    ])
    def test_blocked(self, dns, url):
        with pytest.raises(BlockedDestination):
            check_url(url)

    @pytest.mark.parametrize("url", [
        "https://example.com/unsub?t=abc",
        "http://example.com/u",
        "https://example.com:443/u",
        "http://example.com:8080/u",
        "https://example.com:8443/u",
        "https://93.184.216.34/u",
    ])
    def test_allowed(self, dns, url):
        check_url(url)
        dns.assert_not_called()

    def test_reason_does_not_leak_url_tokens(self):
        with pytest.raises(BlockedDestination) as info:
            check_url("http://127.0.0.1/unsub?token=SECRET")
        assert "SECRET" not in str(info.value)


# ── Guarded backend / transport ──────────────────────────────────────────────

class _RecordingBackend(httpcore.MockBackend):
    """httpcore MockBackend that records the host it was asked to connect to."""

    def __init__(self, buffer):
        super().__init__(buffer)
        self.hosts = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.hosts.append(host)
        return super().connect_tcp(host, port, timeout, local_address, socket_options)


class TestGuardedBackend:
    def test_connects_to_vetted_ip_not_hostname(self, dns):
        _answer(dns, "93.184.216.34")
        inner = MagicMock()
        GuardedBackend(inner).connect_tcp("unsub.example.com", 443, timeout=5)
        inner.connect_tcp.assert_called_once()
        assert inner.connect_tcp.call_args.args[:2] == ("93.184.216.34", 443)

    def test_private_answer_never_connects(self, dns):
        _answer(dns, "10.0.0.5")
        inner = MagicMock()
        with pytest.raises(BlockedDestination):
            GuardedBackend(inner).connect_tcp("rebind.example.com", 80)
        inner.connect_tcp.assert_not_called()

    def test_tries_next_address_on_connect_error(self, dns):
        _answer(dns, "93.184.216.34", "93.184.216.35")
        inner = MagicMock()
        inner.connect_tcp.side_effect = [httpcore.ConnectError("refused"), "stream"]
        assert GuardedBackend(inner).connect_tcp("unsub.example.com", 443) == "stream"
        assert [c.args[0] for c in inner.connect_tcp.call_args_list] == \
            ["93.184.216.34", "93.184.216.35"]

    def test_unix_sockets_refused(self):
        with pytest.raises(BlockedDestination):
            GuardedBackend(MagicMock()).connect_unix_socket("/var/run/docker.sock")


class TestGuardedTransport:
    def _client(self, inner):
        return httpx.Client(transport=GuardedTransport(GuardedBackend(inner)), trust_env=False)

    def test_request_goes_to_vetted_ip(self, dns):
        _answer(dns, "93.184.216.34")
        inner = _RecordingBackend([b"HTTP/1.1 204 No Content\r\n", b"\r\n"])
        with self._client(inner) as client:
            resp = client.get("http://unsub.example.com/u")
        assert resp.status_code == 204
        assert inner.hosts == ["93.184.216.34"]

    def test_private_resolution_raises_blocked(self, dns):
        _answer(dns, "169.254.169.254")
        inner = _RecordingBackend([])
        with self._client(inner) as client, pytest.raises(BlockedDestination):
            client.get("http://metadata.attacker.example/computeMetadata/v1/")
        assert inner.hosts == []

    def test_httpcore_errors_mapped_to_httpx(self, dns):
        _answer(dns, "93.184.216.34")
        inner = MagicMock()
        inner.connect_tcp.side_effect = httpcore.ConnectTimeout("slow")
        with self._client(inner) as client, pytest.raises(httpx.ConnectTimeout):
            client.get("http://unsub.example.com/u")
