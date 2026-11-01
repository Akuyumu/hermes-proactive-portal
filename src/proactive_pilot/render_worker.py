"""Isolated render worker: one public URL, bounded, fail-closed browser.

This file is executed as a STANDALONE child process by
:mod:`proactive_pilot.native_renderer`; nothing here reads owner config, secret
stores, .env files or the owner browser profile. It receives only a public URL
and bounded non-secret limits as JSON on stdin and writes a small structured
result to stdout. stderr is never used for normal results.

Network contract: the browser's own networking is denied by default (dead
proxy, no host resolution escape, QUIC/WebRTC disabled, WebSockets blocked).
Every document/redirect/subresource/XHR request is instead fulfilled ONLY
through the existing :class:`~proactive_pilot.publictransport.PinnedHTTPSTransport`,
which re-validates and re-pins every hop. Non-GET actions, non-HTTPS schemes,
private targets and private redirects are aborted.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

if __package__ in (None, ""):  # executed directly by the parent interpreter
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from proactive_pilot import publicfeeds as pf  # type: ignore[no-redef]
    from proactive_pilot import publictransport as pt  # type: ignore[no-redef]
    from proactive_pilot.publictransport import PinnedHTTPSTransport  # type: ignore[no-redef]
else:  # imported normally (unit tests / package use)
    from . import publicfeeds as pf
    from . import publictransport as pt
    from .publictransport import PinnedHTTPSTransport

_MAX_INPUT_BYTES = 64 * 1024
_DEFAULT_TIMEOUT = 15
_DEFAULT_MAX_OUTPUT = pf.DEFAULT_MAX_BYTES
_DEFAULT_MAX_REQUESTS = 64
_DEFAULT_MAX_NETWORK = 4 * 1024 * 1024

# Defence in depth: even if a request somehow escapes the route mediator it
# cannot reach the network directly. No owner profile/storage state is used.
_LAUNCH_ARGS = (
    "--proxy-server=http://127.0.0.1:9",
    "--proxy-bypass-list=<-loopback>",
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--disable-quic",
    "--disable-webrtc",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-extensions",
    "--disable-plugins",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
)

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "set-cookie", "set-cookie2",
    "content-length", "content-encoding",
}


class RenderWorkerError(RuntimeError):
    """A bounded, non-leaking worker failure."""


class _BudgetExceeded(Exception):
    pass


class _RejectedRequest(Exception):
    pass


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _positive_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def deny_websocket(websocket_route: Any) -> None:
    """Nonblocking WebSocket deny handler.

    Playwright's routed WebSocket does NOT connect to the server unless
    ``connect_to_server`` is called. Registering a no-op message handler lets
    Playwright keep a mocked/browser-side socket that blackholes outbound
    messages. We deliberately never call ``connect_to_server`` or ``send``:
    that is the supported way to deny WebSockets without the synchronous
    close deadlock.
    """
    on_message = getattr(websocket_route, "on_message", None)
    if callable(on_message):
        on_message(lambda message: None)


def launch_args() -> list[str]:
    """Expose the exact fail-closed Chromium launch flags for focused tests."""
    return list(_LAUNCH_ARGS)


def context_options() -> dict[str, Any]:
    """Fresh non-persistent context options (no storage state/session reuse)."""
    return {
        "service_workers": "block",
        "accept_downloads": False,
        "java_script_enabled": True,
        "ignore_https_errors": False,
        "user_agent": pf.USER_AGENT,
    }


def _safe_response_headers(headers: Any) -> dict[str, str]:
    """Strip cookies and hop-by-hop/encoding headers from a fulfilled response."""
    safe: dict[str, str] = {}
    if not isinstance(headers, dict):
        return safe
    for key, value in headers.items():
        name = str(key).strip()
        lowered = name.lower()
        if not name or lowered in _HOP_BY_HOP:
            continue
        safe[name] = str(value)
    return safe


class NetworkBudget:
    """Per-render request and cumulative response-body byte caps."""

    def __init__(self, max_requests: int, max_network_bytes: int):
        self.max_requests = _positive_int(max_requests, _DEFAULT_MAX_REQUESTS)
        self.max_network_bytes = _positive_int(max_network_bytes, _DEFAULT_MAX_NETWORK)
        self.requests = 0
        self.network_bytes = 0
        self.exceeded = False

    def ensure_available(self) -> None:
        """Reject BEFORE any fetch once either cumulative cap is exhausted."""
        if self.requests >= self.max_requests:
            self.exceeded = True
            raise _BudgetExceeded("request ceiling exhausted")
        if self.remaining_bytes <= 0:
            self.exceeded = True
            raise _BudgetExceeded("network byte ceiling exhausted")

    def begin_request(self) -> None:
        self.requests += 1
        if self.requests > self.max_requests:
            self.exceeded = True
            raise _BudgetExceeded("request ceiling exceeded")

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.max_network_bytes - self.network_bytes)

    def charge(self, size: int) -> None:
        self.network_bytes += max(0, int(size))
        if self.network_bytes > self.max_network_bytes:
            self.exceeded = True
            raise _BudgetExceeded("network byte ceiling exceeded")


class BoundedPinnedTransport(PinnedHTTPSTransport):
    """Pinned transport with strict cumulative per-hop request/byte accounting.

    Every redirect hop is charged as its own request and its response bytes are
    charged against ONE cumulative budget before the next hop is attempted. When
    the budget is exhausted, no further connect/fetch is started. Only response
    body bytes are counted: TLS/header wire bytes are NOT claimed.
    """

    def __init__(self, *, budget: NetworkBudget, **kwargs: Any):
        super().__init__(**kwargs)
        self.budget = budget

    def fetch(self, url: str, **kwargs: Any) -> pf.FetchResponse:
        timeout = int(kwargs.get("timeout", pf.DEFAULT_TIMEOUT))
        max_bytes = int(kwargs.get("max_bytes", pf.DEFAULT_MAX_BYTES))
        generic_opt_in = bool(kwargs.get("generic_opt_in", True))
        allowed_hosts = kwargs.get("allowed_hosts")
        headers = {
            "User-Agent": pf.USER_AGENT,
            "Accept": "application/atom+xml, application/rss+xml, "
                      "application/json, text/xml, text/html, */*",
        }
        etag = str(kwargs.get("etag") or "")
        last_modified = str(kwargs.get("last_modified") or "")
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        current = str(url or "")
        for hop in range(self.max_redirects + 1):
            # Fail BEFORE any DNS/connect when the cumulative budget is spent.
            self.budget.ensure_available()
            vetted = self._vet(current, allowed_hosts=allowed_hosts,
                               generic_opt_in=generic_opt_in)
            parsed = pt.urlsplit(vetted)
            host = pf.canonical_host(parsed.hostname or "")
            port = int(parsed.port or 443)
            addresses = pt.resolve_public_ips(host, resolver=self.resolver)
            hop_limit = min(max(1, max_bytes), self.budget.remaining_bytes)
            if hop_limit <= 0:
                raise _BudgetExceeded("network byte ceiling exhausted")
            self.budget.begin_request()
            status, resp_headers, body, location = self._exchange(
                host, port, addresses, parsed, headers, timeout, hop_limit)
            # Pinned transport reads ``hop_limit + 1`` to detect overrun and
            # clips the body. Charge that extra probe byte conservatively.
            charged = len(body) + (1 if len(body) >= hop_limit else 0)
            self.budget.charge(charged)
            if status == 304:
                return pf.FetchResponse(304, resp_headers, b"", vetted)
            if status in pt._REDIRECT_STATUSES and location:
                if hop >= self.max_redirects:
                    raise pf.FetchFailed("too many redirects")
                current = pt.urljoin(vetted, str(location))
                if len(current) > self.max_url_chars:
                    raise pt.TransportRejected("redirect url too long")
                continue
            return pf.FetchResponse(status, resp_headers, body, vetted)
        raise pf.FetchFailed("too many redirects")


class BrowserRequestMediator:
    """Fulfil browser traffic exclusively through the bounded pinned transport."""

    def __init__(self, transport: Any, budget: NetworkBudget, *, timeout: float):
        self.transport = transport
        self.budget = budget
        self.timeout = max(1, int(timeout))
        self.blocked_reason = ""

    def handle(self, route: Any) -> None:
        try:
            request = getattr(route, "request", None)
            method = str(getattr(request, "method", "GET") or "GET").upper()
            if method != "GET":
                raise _RejectedRequest("non-GET action refused")
            url = str(getattr(request, "url", "") or "")
            parsed = urlsplit(url)
            if parsed.scheme.lower() != "https":
                raise _RejectedRequest("non-HTTPS browser request refused")
            # The bounded transport performs the pre-fetch exhaust check and
            # charges every redirect hop.
            response = self.transport.fetch(
                url, timeout=self.timeout, generic_opt_in=True)
            body = bytes(getattr(response, "body", b"") or b"")
            status = int(getattr(response, "status", 0) or 0)
            if status in {301, 302, 303, 307, 308}:
                raise _RejectedRequest("unmediated redirect refused")
            headers = _safe_response_headers(getattr(response, "headers", {}))
            route.fulfill(status=status, headers=headers, body=body)
        except Exception as exc:  # noqa: BLE001 - any failure aborts this request
            if not self.blocked_reason:
                self.blocked_reason = str(exc)[:120] or "browser request refused"
            try:
                route.abort()
            except Exception:  # noqa: BLE001 - best effort
                pass


def _default_transport(budget: NetworkBudget | None = None) -> Any:
    return BoundedPinnedTransport(budget=budget or NetworkBudget(
        _DEFAULT_MAX_REQUESTS, _DEFAULT_MAX_NETWORK))



def _default_playwright() -> Any:
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _validate_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RenderWorkerError("invalid render request")
    url = str(payload.get("url") or "")
    try:
        url = pf.validate_public_url(url, generic_opt_in=True).geturl()
    except pf.URLRejected as exc:
        raise RenderWorkerError(f"render url rejected: {exc}") from exc
    timeout = min(_positive_float(payload.get("timeout"), _DEFAULT_TIMEOUT), 60.0)
    return {
        "url": url,
        "timeout": timeout,
        "max_output_bytes": min(
            _positive_int(payload.get("max_output_bytes"), _DEFAULT_MAX_OUTPUT),
            _DEFAULT_MAX_OUTPUT,
        ),
        "max_requests": _positive_int(
            payload.get("max_requests"), _DEFAULT_MAX_REQUESTS),
        "max_network_bytes": _positive_int(
            payload.get("max_network_bytes"), _DEFAULT_MAX_NETWORK),
    }


def render_public_url(
    payload: Any,
    *,
    transport_factory: Callable[[], Any] | None = None,
    playwright_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Render one vetted public URL in a fresh isolated context.

    Factories are injectable so focused tests stay offline (no real browser,
    socket, resolver or network).
    """
    cfg = _validate_payload(payload)
    budget = NetworkBudget(cfg["max_requests"], cfg["max_network_bytes"])
    transport = (transport_factory() if transport_factory is not None
                 else _default_transport(budget))
    mediator = BrowserRequestMediator(transport, budget, timeout=cfg["timeout"])
    timeout_ms = max(1000, int(cfg["timeout"] * 1000))
    browser = None
    context = None
    try:
        with (playwright_factory or _default_playwright)() as playwright:
            browser = playwright.chromium.launch(
                headless=True, chromium_sandbox=True, args=launch_args())
            context = browser.new_context(**context_options())
            context_route = getattr(context, "route", None)
            if not callable(context_route):
                raise RenderWorkerError("context-level request mediation unavailable")
            # BrowserContext-level mediation covers the first navigation of every
            # page/popup in this context before untrusted content runs.
            context_route("**/*", mediator.handle)
            ws_route = getattr(context, "route_web_socket", None)
            page = context.new_page()
            if not callable(ws_route):
                ws_route = getattr(page, "route_web_socket", None)
            if not callable(ws_route):
                # Fail closed rather than silently ignoring the required hook.
                raise RenderWorkerError("websocket mediation unavailable")
            try:
                ws_route("**/*", deny_websocket)
            except Exception as exc:  # noqa: BLE001 - required API must work
                raise RenderWorkerError("websocket mediation unavailable") from exc
            try:
                context.on("page", lambda popup: popup.close())
            except Exception:  # noqa: BLE001 - popup suppression is best effort
                pass
            try:
                page.set_default_timeout(timeout_ms)
                page.set_default_navigation_timeout(timeout_ms)
            except Exception:  # noqa: BLE001 - older/newer API differences
                pass
            try:
                page.on("download", lambda download: download.cancel())
            except Exception:  # noqa: BLE001 - best effort
                pass
            try:
                page.goto(cfg["url"], wait_until="domcontentloaded",
                          timeout=timeout_ms)
                try:
                    page.wait_for_load_state(
                        "networkidle", timeout=min(timeout_ms, 1500))
                except Exception:  # noqa: BLE001 - bounded best-effort settling
                    pass
            except Exception as exc:  # noqa: BLE001 - do not leak browser detail
                if budget.exceeded:
                    raise RenderWorkerError("render request budget exceeded") from exc
                raise RenderWorkerError("render navigation failed") from exc
            if budget.exceeded:
                raise RenderWorkerError("render request budget exceeded")
            html = str(page.content() or "")
            body = html.encode("utf-8", "replace")[: cfg["max_output_bytes"]]
            if not body:
                raise RenderWorkerError("renderer returned no content")
            return {
                "ok": True,
                "body_b64": base64.b64encode(body).decode("ascii"),
            }
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - best effort
                pass


def main() -> int:
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        result: dict[str, Any] = {"ok": False, "error": "render request too large"}
    else:
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
            result = render_public_url(payload)
        except RenderWorkerError as exc:
            result = {"ok": False, "error": str(exc)[:200]}
        except Exception:  # noqa: BLE001 - curated, non-leaking failure
            result = {"ok": False, "error": "native render unavailable"}
    try:
        sys.stdout.buffer.write(json.dumps(
            result, separators=(",", ":")).encode("utf-8"))
        sys.stdout.buffer.flush()
    except Exception:  # noqa: BLE001 - stdout is the only result channel
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
