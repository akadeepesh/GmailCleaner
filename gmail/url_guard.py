"""
GmailCleaner — outbound URL guard for unsubscribe requests.

List-Unsubscribe URLs are chosen by whoever sent the email, so they are
untrusted. Without a guard, a sender could point one at the cloud metadata
server (169.254.169.254), the local dashboard (127.0.0.1) or any other
internal address, and GmailCleaner would request it, possibly unattended
via a trusted rule.

Two layers:

  * check_url()       Cheap, DNS-free checks on the URL itself (scheme,
                      userinfo, port, IP literals, obviously internal names).
                      Runs on the initial URL and on every redirect hop.
  * GuardedBackend    An httpcore network backend that resolves the host
                      itself, rejects it if ANY address is non-public, and
                      then connects to one of the addresses it checked. The
                      HTTP stack never re-resolves the name, so DNS rebinding
                      (public IP at check time, private IP at connect time)
                      does not work. TLS still uses the original hostname for
                      SNI and certificate checks, because httpcore passes it
                      separately to start_tls().

GuardedTransport plugs GuardedBackend into an httpx.Client through public
APIs only (httpx.BaseTransport + httpcore.ConnectionPool(network_backend=)).

This module has no Gmail or database imports.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from typing import Iterable, Iterator, Optional
from urllib.parse import urlsplit

import httpcore
import httpx

# Ports an unsubscribe URL may use (None = the scheme's default).
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
ALLOWED_SCHEMES = frozenset({"http", "https"})

# Names that are internal by convention, whatever DNS says.
_INTERNAL_NAMES = frozenset({"localhost", "metadata.google.internal"})
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan")

# NAT64 prefixes embed an IPv4 address in the low 32 bits.
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.ip_network("64:ff9b:1::/48")


class BlockedDestination(Exception):
    """The URL or the address it resolves to is not a safe public destination.

    Messages name the host at most, never the full URL (which can carry tokens).
    """


# ──────────────────────────────────────────────────────────────────────────────
# Address and URL checks
# ──────────────────────────────────────────────────────────────────────────────

def is_public_ip(ip: str) -> bool:
    """True only for a syntactically valid, globally routable unicast address."""
    if not isinstance(ip, str) or "%" in ip:  # IPv6 zone ids
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return _is_public_addr(addr)


def _is_public_addr(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if addr.is_multicast or not addr.is_global:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        # Forms that embed an IPv4 address: judge the embedded address too.
        # (Python < 3.13 does not do this for ::ffff:a.b.c.d on its own.)
        if addr.ipv4_mapped is not None:
            return _is_public_addr(addr.ipv4_mapped)
        if addr.sixtofour is not None and not _is_public_addr(addr.sixtofour):
            return False
        if addr.teredo is not None and not _is_public_addr(addr.teredo[1]):
            return False
        if addr in _NAT64_LOCAL_USE:
            return False
        if addr in _NAT64_WELL_KNOWN:
            embedded = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
            return _is_public_addr(embedded)
    return True


def _clean_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


def _as_ip_literal(host: str) -> Optional[str]:
    """Return the canonical IP if `host` is any IP spelling, else None.

    Besides normal IPv4/IPv6 literals this catches the legacy forms that
    inet_aton accepts and resolvers honour: 2130706433, 0x7f.0.0.1, 127.1, 0177.0.0.1.
    """
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except (OSError, ValueError, UnicodeError):
        return None


def _is_internal_name(host: str) -> bool:
    return (
        not host
        or host in _INTERNAL_NAMES
        or host.endswith(_INTERNAL_SUFFIXES)
        or "." not in host  # single-label names (intranet, printer, ...)
    )


def _check_host_static(host: str) -> Optional[str]:
    """DNS-free host check. Returns the IP if the host is a (public) literal."""
    if not host or "%" in host:
        raise BlockedDestination("missing or malformed host")
    literal = _as_ip_literal(host)
    if literal is not None:
        if not is_public_ip(literal):
            raise BlockedDestination(f"{literal} is not a public address")
        return literal
    if _is_internal_name(host):
        raise BlockedDestination(f"host '{host}' is internal")
    return None


def resolve_public(host: str, port: int) -> list[str]:
    """Resolve `host` and return its addresses, only if every one is public.

    Raises BlockedDestination for internal names, unresolvable hosts, or if
    ANY resolved address is non-public (a name mixing public and private
    records is treated as hostile).
    """
    host = _clean_host(host)
    literal = _check_host_static(host)
    if literal is not None:
        return [literal]

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise BlockedDestination(f"host '{host}' does not resolve") from exc

    addresses: list[str] = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in addresses:
            addresses.append(ip)
    if not addresses:
        raise BlockedDestination(f"host '{host}' does not resolve")
    for ip in addresses:
        if not is_public_ip(ip):
            raise BlockedDestination(f"host '{host}' resolves to non-public address {ip}")
    return addresses


def check_url(url: str) -> None:
    """DNS-free validation of an unsubscribe URL. Raises BlockedDestination."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except (ValueError, TypeError) as exc:
        raise BlockedDestination("malformed URL") from exc

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedDestination("scheme must be http or https")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise BlockedDestination("credentials in URL are not allowed")
    if port is not None and port not in ALLOWED_PORTS:
        raise BlockedDestination(f"port {port} is not allowed")
    _check_host_static(_clean_host(parts.hostname or ""))


# ──────────────────────────────────────────────────────────────────────────────
# httpcore backend + httpx transport that connect only to checked addresses
# ──────────────────────────────────────────────────────────────────────────────

class GuardedBackend(httpcore.NetworkBackend):
    """Network backend that resolves and vets the host before connecting.

    It connects to the vetted IP, never to the hostname, so the address
    that was checked is the address used.
    """

    def __init__(self, inner: Optional[httpcore.NetworkBackend] = None) -> None:
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Optional[Iterable] = None,
    ) -> httpcore.NetworkStream:
        addresses = resolve_public(host, port)
        last_exc: Optional[Exception] = None
        for ip in addresses:
            try:
                return self._inner.connect_tcp(
                    ip, port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise BlockedDestination("unix sockets are not allowed")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


# httpcore -> httpx exception mapping (most specific first).
_EXC_MAP: tuple[tuple[type[Exception], type[Exception]], ...] = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.TimeoutException, httpx.TimeoutException),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.NetworkError, httpx.NetworkError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.ProtocolError, httpx.ProtocolError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
)


@contextlib.contextmanager
def _map_httpcore_errors() -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        for src, dst in _EXC_MAP:
            if isinstance(exc, src):
                raise dst(str(exc)) from exc
        raise


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream: Iterable[bytes]) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        with _map_httpcore_errors():
            for chunk in self._stream:
                yield chunk

    def close(self) -> None:
        if hasattr(self._stream, "close"):
            self._stream.close()


class GuardedTransport(httpx.BaseTransport):
    """httpx transport over an httpcore pool that uses GuardedBackend.

    Proxies are deliberately unsupported: through a proxy the connect-time
    check would only ever see the proxy's address.
    """

    def __init__(self, network_backend: Optional[httpcore.NetworkBackend] = None) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=network_backend or GuardedBackend(),
            http1=True,
            http2=False,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.SyncByteStream)
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_errors():
            resp = self._pool.handle_request(core_request)
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_ResponseStream(resp.stream),
            extensions=resp.extensions,
        )

    def close(self) -> None:
        self._pool.close()
