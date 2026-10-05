"""Vetted public HTTPS transport with DNS pinning (SSRF/rebinding safe).

The ordinary feed fetcher (:func:`publicfeeds.fetch_bytes`) validates a URL and
then hands it to a generic urllib opener which performs its **own** DNS
resolution at connect time. That leaves a TOCTOU gap: a hostname can pass the
pre-flight public-IP check and then re-resolve to a private address when the
socket is opened (DNS rebinding), and a redirect can pivot to a private target
between the pre-check and the actual connect.

This module closes that gap for dynamically discovered sources:

- Every resolved A/AAAA address must be globally routable (``ip.is_global``),
  with mapped/private/loopback/link-local/reserved/unspecified and CGNAT
  addresses refused. If ANY address is not public the URL is rejected.
- The socket is opened to **one validated pinned address**, not to the
  hostname, so the connect cannot re-resolve. TLS still uses the original
  hostname for SNI and certificate/hostname validation (``server_hostname``),
  and the HTTP ``Host`` header stays the original host.
- Every redirect hop is re-validated and re-resolved, then re-pinned. There is
  no fallback to a generic opener that would bypass the checks.

No live request is made in this stage; tests inject a resolver and a fake
connector and never touch a socket.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from . import publicfeeds as pf

DEFAULT_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
# Address blocks that are not globally routable but which ``is_global`` alone
# may not exclude on every interpreter (notably IPv4 CGNAT).
_EXTRA_NON_PUBLIC = (
    ipaddress.ip_network("100.64.0.0/10"),  # carrier-grade NAT
)
_MAX_URL_CHARS = 2048


class TransportRejected(pf.URLRejected):
    """A discovered URL failed pinned-transport vetting before any connect."""


def ip_is_global(address: str) -> bool:
    """True only for a globally routable address (never private/CGNAT/etc.)."""
    try:
        ip = ipaddress.ip_address(str(address).strip())
    except ValueError:
        return False
    # Any IPv4-mapped IPv6 form is refused outright -- even one embedding a
    # public IPv4 -- so behaviour matches the docstring and no mapped alias can
    # slip a non-obvious address past the pre-flight vetting.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return False
    if not ip.is_global:
        return False
    for net in _EXTRA_NON_PUBLIC:
        if ip.version == net.version and ip in net:
            return False
    return True


def resolve_public_ips(
    host: str,
    *,
    resolver: Callable[[str], list[str]] | None = None,
) -> list[str]:
    """Resolve a host to its addresses, failing closed on any non-public one."""
    host = pf.canonical_host(host)
    if not host:
        raise TransportRejected("missing host")
    if resolver is not None:
        try:
            addresses = [str(a) for a in resolver(host)]
        except TransportRejected:
            raise
        except Exception as exc:  # noqa: BLE001 - resolver failure fails closed
            raise TransportRejected("dns resolution failed") from exc
    else:
        try:
            infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, OSError) as exc:
            raise TransportRejected("dns resolution failed") from exc
        addresses = [info[4][0] for info in infos]
    if not addresses:
        raise TransportRejected("host resolved to no address")
    for address in addresses:
        if not ip_is_global(address):
            raise TransportRejected("host resolves to a non-public address")
    return addresses


def vet_public_url(
    url: str,
    *,
    resolver: Callable[[str], list[str]] | None = None,
) -> str:
    """Validate a PUBLIC https URL and require every resolved IP to be global."""
    if len(str(url or "")) > _MAX_URL_CHARS:
        raise TransportRejected("url too long")
    try:
        parsed = pf.validate_public_url(url, generic_opt_in=True)
    except pf.URLRejected as exc:
        raise TransportRejected(str(exc)) from exc
    resolve_public_ips(parsed.hostname or "", resolver=resolver)
    return parsed.geturl()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects to a pinned IP but keeps the real host.

    ``host`` is used for the ``Host`` header and for TLS SNI / certificate
    hostname validation; only the TCP destination is overridden to ``pin_ip``.
    """

    def __init__(self, host: str, *, pin_ip: str, **kwargs: Any):
        super().__init__(host, **kwargs)
        self._pin_ip = pin_ip
        # HTTPConnection.__init__ installs an instance attribute which shadows
        # class methods. Rebind after super(), or connect would resolve host again.
        self._create_connection = self._connect_pinned

    def _connect_pinned(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                           source_address=None):  # type: ignore[override]
        # Ignore the hostname in ``address`` entirely: connect to the pinned,
        # already-validated address so the socket cannot re-resolve.
        return socket.create_connection((self._pin_ip, address[1]), timeout, source_address)


class PinnedHTTPSTransport:
    """Bounded HTTPS fetcher that pins every connect to a vetted public IP.

    ``connector`` is injectable: it receives ``(host, port, ip, timeout)`` and
    must return an object with ``request``/``getresponse`` (an
    :class:`http.client.HTTPSConnection` by default). Tests pass a fake so no
    socket is ever opened.
    """

    def __init__(
        self,
        *,
        resolver: Callable[[str], list[str]] | None = None,
        connector: Callable[[str, int, str, int], Any] | None = None,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_url_chars: int = _MAX_URL_CHARS,
    ):
        self.resolver = resolver
        self.connector = connector or self._default_connector
        self.max_redirects = max(0, int(max_redirects))
        self.max_url_chars = max(64, int(max_url_chars))

    # -- default (real) connector -----------------------------------------
    def _default_connector(self, host: str, port: int, ip: str, timeout: int) -> Any:
        context = ssl.create_default_context()
        conn = _PinnedHTTPSConnection(
            host, port=port, timeout=max(1, int(timeout)), context=context, pin_ip=ip)
        conn.connect()
        return conn

    # -- public API --------------------------------------------------------
    def fetch(self, url: str, **kwargs: Any) -> pf.FetchResponse:
        """Fetch a vetted public URL, re-pinning after every redirect hop."""
        timeout = int(kwargs.get("timeout", pf.DEFAULT_TIMEOUT))
        max_bytes = int(kwargs.get("max_bytes", pf.DEFAULT_MAX_BYTES))
        generic_opt_in = bool(kwargs.get("generic_opt_in", True))
        allowed_hosts = kwargs.get("allowed_hosts")
        headers = {"User-Agent": pf.USER_AGENT,
                   "Accept": "application/atom+xml, application/rss+xml, "
                             "application/json, text/xml, text/html, */*"}
        etag = str(kwargs.get("etag") or "")
        last_modified = str(kwargs.get("last_modified") or "")
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        current = str(url or "")
        for hop in range(self.max_redirects + 1):
            vetted = self._vet(current, allowed_hosts=allowed_hosts,
                               generic_opt_in=generic_opt_in)
            parsed = urlsplit(vetted)
            host = pf.canonical_host(parsed.hostname or "")
            port = int(parsed.port or 443)
            addresses = resolve_public_ips(host, resolver=self.resolver)
            status, resp_headers, body, location = self._exchange(
                host, port, addresses, parsed, headers, timeout, max_bytes)
            if status == 304:
                return pf.FetchResponse(304, resp_headers, b"", vetted)
            if status in _REDIRECT_STATUSES and location:
                if hop >= self.max_redirects:
                    raise pf.FetchFailed("too many redirects")
                # The next loop re-validates AND re-resolves the new target,
                # so a redirect cannot bypass the pinned public check.
                current = urljoin(vetted, str(location))
                if len(current) > self.max_url_chars:
                    raise TransportRejected("redirect url too long")
                continue
            return pf.FetchResponse(status, resp_headers, body, vetted)
        raise pf.FetchFailed("too many redirects")

    # -- internals ---------------------------------------------------------
    def _vet(self, url: str, *, allowed_hosts: Any = None,
             generic_opt_in: bool = True) -> str:
        if len(str(url or "")) > self.max_url_chars:
            raise TransportRejected("url too long")
        try:
            parsed = pf.validate_public_url(
                url, allowed_hosts=allowed_hosts, generic_opt_in=generic_opt_in)
        except pf.URLRejected as exc:
            raise TransportRejected(str(exc)) from exc
        if not parsed.hostname:
            raise TransportRejected("missing host")
        return parsed.geturl()

    def _exchange(self, host: str, port: int, addresses: list[str], parsed: Any,
                  headers: dict[str, str], timeout: int, max_bytes: int
                  ) -> tuple[int, dict[str, str], bytes, str]:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        last_error: Exception | None = None
        for address in addresses:
            try:
                conn = self.connector(host, port, address, timeout)
                try:
                    conn.request("GET", path, headers=headers)
                    response = conn.getresponse()
                    status = int(getattr(response, "status", 0) or 0)
                    resp_headers = {str(k): str(v) for k, v in
                                    dict(getattr(response, "headers", {}) or {}).items()}
                    raw = response.read(max_bytes + 1)
                    if len(raw) > max_bytes:
                        raw = raw[:max_bytes]
                    location = ""
                    if status in _REDIRECT_STATUSES:
                        for key, value in resp_headers.items():
                            if key.lower() == "location" and value:
                                location = value
                                break
                    return status, resp_headers, raw, location
                finally:
                    close = getattr(conn, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:  # noqa: BLE001 - best-effort close
                            pass
            except TransportRejected:
                raise
            except (pf.FetchFailed, pf.URLRejected):
                raise
            except Exception as exc:  # noqa: BLE001 - try the next pinned IP
                last_error = exc
                continue
        raise pf.FetchFailed("network error: pinned connect failed") from last_error


def default_transport(**kwargs: Any) -> PinnedHTTPSTransport:
    return PinnedHTTPSTransport(**kwargs)
