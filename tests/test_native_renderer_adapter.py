"""Focused offline tests for the opt-in plugin-owned native renderer.

Everything here is SYNTHETIC: no socket, no real browser, no resolver, no
network, no model and no owner profile. Real public rendering acceptance is the
parent's harness, not this file.
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import native_renderer as nr  # noqa: E402
from proactive_pilot import publictransport as pt  # noqa: E402
from proactive_pilot import render_worker as rw  # noqa: E402
from proactive_pilot import routed_retrieval as rr  # noqa: E402

PAGE_URL = "https://fixtures.example.org/page-1"
PRIVATE_URL = "https://10.0.0.5/hidden"
PUBLIC_IP = "93.184.216.34"


def _flags(**over):
    policy = {
        "native_render_fallback_enabled": True,
        "native_render_consent": True,
        "retrieval_fallback_max_bytes": 2_000_000,
        "native_render_timeout_seconds": 15,
    }
    policy.update(over)
    return policy


class _Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = dict(headers)
        self._body = bytes(body)

    def read(self, size):
        return self._body[:size]


class _Conn:
    def __init__(self, response):
        self._response = response
        self.closed = False

    def request(self, method, path, headers=None):
        self.method = method

    def getresponse(self):
        return self._response

    def close(self):
        self.closed = True


def _transport(*, response, resolver=None, connector=None):
    return pt.PinnedHTTPSTransport(
        resolver=resolver or (lambda host: [PUBLIC_IP]),
        connector=connector or (lambda host, port, ip, timeout: _Conn(response)),
    )


def _bounded_transport(*, responses, resolver=None, max_requests=8, max_bytes=100_000):
    """A budgeted pinned transport with an observable connector call log."""
    budget = rw.NetworkBudget(max_requests, max_bytes)
    calls = []

    def _connector(host, port, ip, timeout):
        calls.append(host)
        response = responses.get(host, responses.get("*"))
        if response is None:
            raise AssertionError(f"unexpected connector call for {host}")
        return _Conn(response)

    transport = rw.BoundedPinnedTransport(
        budget=budget,
        resolver=resolver or (lambda host: [PUBLIC_IP]),
        connector=_connector,
    )
    transport.connector_calls = calls
    return transport, budget


class _Request:
    def __init__(self, url, method="GET"):
        self.url = url
        self.method = method


class _FakeRoute:
    def __init__(self, url, method="GET"):
        self.request = _Request(url, method)
        self.fulfilled = None
        self.aborted = False

    def fulfill(self, **kwargs):
        self.fulfilled = kwargs

    def abort(self):
        self.aborted = True


class MediatorTransportTests(unittest.TestCase):
    def _mediator(self, response, *, resolver=None, max_requests=8, max_bytes=100_000):
        transport, budget = _bounded_transport(
            responses={"*": response}, resolver=resolver,
            max_requests=max_requests, max_bytes=max_bytes)
        mediator = rw.BrowserRequestMediator(transport, budget, timeout=5)
        return mediator

    def test_public_get_is_fulfilled_through_pinned_transport(self):
        mediator = self._mediator(_Response(
            200, {"Content-Type": "text/html", "Set-Cookie": "sid=secret"},
            b"<html>ok</html>"))
        route = _FakeRoute(PAGE_URL)
        mediator.handle(route)
        self.assertFalse(route.aborted)
        self.assertEqual(route.fulfilled["status"], 200)
        self.assertEqual(route.fulfilled["body"], b"<html>ok</html>")
        self.assertNotIn("Set-Cookie", route.fulfilled["headers"])

    def test_non_get_and_unsafe_schemes_are_aborted(self):
        mediator = self._mediator(_Response(200, {}, b"x"))
        for url, method in (
            (PAGE_URL, "POST"), ("http://fixtures.example.org/x", "GET"),
            ("file:///etc/passwd", "GET"), ("ws://fixtures.example.org/s", "GET"),
            ("wss://fixtures.example.org/s", "GET"), ("gopher://x/y", "GET"),
        ):
            with self.subTest(url=url, method=method):
                route = _FakeRoute(url, method)
                mediator.handle(route)
                self.assertTrue(route.aborted)
                self.assertIsNone(route.fulfilled)

    def test_private_url_and_private_dns_are_blocked_by_transport(self):
        for url, resolver in (
            (PRIVATE_URL, None),
            ("https://private.example.org/x", lambda host: ["10.0.0.5"]),
        ):
            with self.subTest(url=url):
                mediator = self._mediator(_Response(200, {}, b"x"), resolver=resolver)
                route = _FakeRoute(url)
                mediator.handle(route)
                self.assertTrue(route.aborted)
                self.assertIsNone(route.fulfilled)

    def test_private_redirect_is_blocked_through_transport_seam(self):
        def resolver(host):
            return [PUBLIC_IP] if host == "fixtures.example.org" else ["10.0.0.5"]

        mediator = self._mediator(
            _Response(302, {"Location": "https://internal.example.org/x"}, b""),
            resolver=resolver)
        route = _FakeRoute(PAGE_URL)
        mediator.handle(route)
        self.assertTrue(route.aborted)
        self.assertIsNone(route.fulfilled)

    def test_byte_exhaustion_rejects_before_any_later_fetch(self):
        transport, _budget = _bounded_transport(
            responses={"*": _Response(200, {}, b"x" * 64)},
            max_requests=8, max_bytes=32)
        mediator = rw.BrowserRequestMediator(transport, transport.budget, timeout=5)
        first = _FakeRoute(PAGE_URL)
        mediator.handle(first)
        # Clipped-body extra byte is charged, so the first response already
        # exhausts the cumulative cap and is not treated as a successful fetch.
        self.assertTrue(first.aborted)
        self.assertGreaterEqual(transport.budget.network_bytes, 32)
        self.assertEqual(len(transport.connector_calls), 1)

        second = _FakeRoute(PAGE_URL + "?next")
        mediator.handle(second)
        self.assertTrue(second.aborted)
        self.assertEqual(len(transport.connector_calls), 1,
                         "no connector call may occur after exhaustion")

    def test_request_cap_rejects_before_any_later_fetch(self):
        transport, _budget = _bounded_transport(
            responses={"*": _Response(200, {}, b"ok")},
            max_requests=1, max_bytes=100_000)
        mediator = rw.BrowserRequestMediator(transport, transport.budget, timeout=5)
        first = _FakeRoute(PAGE_URL)
        mediator.handle(first)
        self.assertFalse(first.aborted)
        self.assertEqual(len(transport.connector_calls), 1)

        second = _FakeRoute(PAGE_URL + "?next")
        mediator.handle(second)
        self.assertTrue(second.aborted)
        self.assertEqual(len(transport.connector_calls), 1,
                         "no connector call may occur after the request cap")

    def test_redirect_hops_are_counted_against_one_cumulative_budget(self):
        responses = {
            "fixtures.example.org": _Response(
                302, {"Location": "https://other.example.org/final"}, b""),
            "other.example.org": _Response(200, {}, b"final-body"),
        }
        transport, _budget = _bounded_transport(
            responses=responses, max_requests=8, max_bytes=100_000)
        mediator = rw.BrowserRequestMediator(transport, transport.budget, timeout=5)
        route = _FakeRoute(PAGE_URL)
        mediator.handle(route)
        self.assertFalse(route.aborted)
        self.assertEqual(route.fulfilled["body"], b"final-body")
        self.assertEqual(transport.budget.requests, 2,
                         "each redirect hop is a counted request")
        self.assertEqual(transport.connector_calls,
                         ["fixtures.example.org", "other.example.org"])

    def test_redirect_hop_is_rejected_when_next_request_budget_is_spent(self):
        responses = {
            "fixtures.example.org": _Response(
                302, {"Location": "https://other.example.org/final"}, b""),
            "other.example.org": _Response(200, {}, b"must-not-be-fetched"),
        }
        transport, _budget = _bounded_transport(
            responses=responses, max_requests=1, max_bytes=100_000)
        mediator = rw.BrowserRequestMediator(transport, transport.budget, timeout=5)
        route = _FakeRoute(PAGE_URL)
        mediator.handle(route)
        self.assertTrue(route.aborted)
        self.assertEqual(transport.connector_calls, ["fixtures.example.org"],
                         "the redirect target must not be fetched after exhaustion")


class _FakePage:
    def __init__(self, html):
        self._html = html
        self.routes = []
        self.handlers = {}
        self.websocket_handlers = []
        self.timeouts = []

    def set_default_timeout(self, value):
        self.timeouts.append(("default", value))

    def set_default_navigation_timeout(self, value):
        self.timeouts.append(("navigation", value))

    def on(self, event, handler):
        self.handlers[event] = handler

    def route_web_socket(self, pattern, handler):
        self.websocket_handlers.append((pattern, handler))

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def goto(self, url, **kwargs):
        self.goto_args = (url, kwargs)

    def wait_for_load_state(self, state, timeout=None):
        self.load_state = (state, timeout)

    def content(self):
        return self._html


class _FakeContext:
    def __init__(self, html):
        self.options = {}
        self.page = _FakePage(html)
        self.routes = []
        self.websocket_handlers = []
        self.handlers = {}
        self.closed = False

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def route_web_socket(self, pattern, handler):
        self.websocket_handlers.append((pattern, handler))

    def on(self, event, handler):
        self.handlers[event] = handler

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.launch_kwargs = None
        self.contexts = []
        self.closed = False

    def new_context(self, **kwargs):
        context = _FakeContext("<html><body>rendered</body></html>")
        context.options = dict(kwargs)
        self.contexts.append(context)
        return context

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __init__(self, browser):
        self.chromium = self
        self._browser = browser

    def launch(self, **kwargs):
        self._browser.launch_kwargs = dict(kwargs)
        return self._browser


class _FakePlaywrightCM:
    def __init__(self, browser):
        self._playwright = _FakePlaywright(browser)

    def __enter__(self):
        return self._playwright

    def __exit__(self, *exc):
        return False


class _NullTransport:
    def fetch(self, *args, **kwargs):
        raise AssertionError("no route should have been exercised by goto fake")


class WorkerIsolationTests(unittest.TestCase):
    def test_render_uses_fresh_blocked_context_and_closes_everything(self):
        browser = _FakeBrowser()
        result = rw.render_public_url(
            {"url": PAGE_URL, "timeout": 5, "max_output_bytes": 100_000},
            transport_factory=lambda: _NullTransport(),
            playwright_factory=lambda: _FakePlaywrightCM(browser),
        )
        self.assertTrue(result["ok"])
        self.assertIn(b"rendered", base64.b64decode(result["body_b64"]))
        self.assertTrue(browser.launch_kwargs["chromium_sandbox"])
        args = browser.launch_kwargs["args"]
        self.assertTrue(any("--proxy-server=" in arg for arg in args))
        self.assertIn("--proxy-bypass-list=<-loopback>", args)
        self.assertIn("--disable-quic", args)
        self.assertTrue(any("webrtc" in arg for arg in args))
        context = browser.contexts[0]
        self.assertEqual(context.options["service_workers"], "block")
        self.assertFalse(context.options["accept_downloads"])
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(context.routes,
                        "BrowserContext-level mediation must cover every page")
        self.assertTrue(context.websocket_handlers,
                        "WebSocket mediation is required, not optional")
        self.assertIn("download", context.page.handlers)

        class _FakeWebSocket:
            def __init__(self):
                self.message_handlers = []
                self.connect_to_server_called = False
                self.send_called = False
                self.close_called = False

            def on_message(self, handler):
                self.message_handlers.append(handler)

            def connect_to_server(self):
                self.connect_to_server_called = True

            def send(self, message):
                self.send_called = True

            def close(self):
                self.close_called = True

        ws = _FakeWebSocket()
        _pattern, handler = context.websocket_handlers[0]
        handler(ws)
        self.assertEqual(len(ws.message_handlers), 1,
                         "a nonblocking message handler must be registered")
        self.assertFalse(ws.connect_to_server_called,
                         "the mocked socket must never connect to a server")
        self.assertFalse(ws.send_called, "the deny handler must never send")
        self.assertFalse(ws.close_called,
                         "synchronous close deadlocks the real sync handler")

    def test_websocket_deny_handler_is_nonblocking_and_never_connects(self):
        class _FakeWebSocket:
            def __init__(self):
                self.message_handlers = []
                self.connect_to_server_called = False
                self.send_called = False

            def on_message(self, handler):
                self.message_handlers.append(handler)

            def connect_to_server(self):
                self.connect_to_server_called = True

            def send(self, message):
                self.send_called = True

        ws = _FakeWebSocket()
        self.assertIsNone(rw.deny_websocket(ws))
        self.assertEqual(len(ws.message_handlers), 1)
        self.assertFalse(ws.connect_to_server_called)
        self.assertFalse(ws.send_called)
        # A blank/None message must be handled without touching the network.
        ws.message_handlers[0](None)

    def test_worker_rejects_private_url_before_browser_launch(self):
        called = {"browser": False}

        def factory():
            called["browser"] = True
            return _FakePlaywrightCM(_FakeBrowser())

        with self.assertRaises(rw.RenderWorkerError):
            rw.render_public_url(
                {"url": PRIVATE_URL, "timeout": 5},
                transport_factory=lambda: _NullTransport(),
                playwright_factory=factory,
            )
        self.assertFalse(called["browser"])


class _RecordingRunner:
    def __init__(self, body=b"<html>ok</html>"):
        self.body = body
        self.calls = []

    def __call__(self, payload, **kwargs):
        self.calls.append((dict(payload), dict(kwargs)))
        return json.dumps({
            "ok": True,
            "body_b64": base64.b64encode(self.body).decode("ascii"),
        }).encode("utf-8")


class RendererAdapterTests(unittest.TestCase):
    def test_build_and_availability_are_neutral_by_default(self):
        self.assertIsNone(nr.build_native_renderer({}))
        ok, reason = nr.native_renderer_availability({})
        self.assertFalse(ok)
        self.assertIn("disabled", reason)
        only_enabled = {"native_render_fallback_enabled": True}
        self.assertIsNone(nr.build_native_renderer(only_enabled))
        self.assertFalse(nr.native_renderer_availability(only_enabled)[0])

    def test_both_flags_build_the_plugin_owned_adapter(self):
        renderer = nr.build_native_renderer(_flags())
        self.assertIsInstance(renderer, nr.NativePublicRenderer)
        ok, reason = nr.native_renderer_availability(_flags())
        self.assertTrue(ok, reason)
        self.assertIn("configured/eligible", reason)
        self.assertIn("not verified", reason)

    def test_explicit_nonpositive_or_nonfinite_timeout_fails_before_spawn(self):
        runner = _RecordingRunner()
        renderer = nr.NativePublicRenderer(_flags(), runner=runner)
        with mock.patch.object(nr.tempfile, "mkdtemp",
                               side_effect=AssertionError("must not create tempdir")):
            for value in (0, -1, 0.0, -0.5, float("inf"), float("nan"), "bad"):
                with self.subTest(timeout=value):
                    with self.assertRaises(nr.RenderUnavailable):
                        renderer.render(PAGE_URL, timeout=value)
        self.assertEqual(runner.calls, [])

    def test_omitted_timeout_uses_configured_default(self):
        runner = _RecordingRunner()
        renderer = nr.NativePublicRenderer(_flags(), runner=runner)
        renderer.render(PAGE_URL)
        self.assertEqual(runner.calls[0][0]["timeout"], 15)

    def test_tempdir_base_continues_to_use_supplied_tmpdir(self):
        runner = _RecordingRunner()
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as base:
            renderer = nr.NativePublicRenderer(
                _flags(native_render_tmpdir=base), runner=runner)
            renderer.render(PAGE_URL)
        self.assertTrue(Path(runner.calls[0][1]["tmpdir"]).parent == Path(base))

    def test_private_and_malformed_urls_never_spawn_a_worker(self):
        runner = _RecordingRunner()
        renderer = nr.NativePublicRenderer(_flags(), runner=runner)
        for url in (PRIVATE_URL, "http://fixtures.example.org/x", "not a url",
                    "file:///etc/passwd"):
            with self.subTest(url=url):
                with self.assertRaises(nr.RenderUnavailable):
                    renderer.render(url)
        self.assertEqual(runner.calls, [])

    def test_caller_residual_timeout_is_respected(self):
        runner = _RecordingRunner()
        renderer = nr.NativePublicRenderer(_flags(), runner=runner)
        renderer.render(PAGE_URL, timeout=2, max_bytes=500_000)
        payload, kwargs = runner.calls[0]
        self.assertLessEqual(payload["timeout"], 2)
        self.assertLessEqual(kwargs["timeout"], 2)
        self.assertEqual(payload["max_output_bytes"], 500_000)

    def test_rendered_output_is_capped(self):
        runner = _RecordingRunner(body=b"A" * 100)
        renderer = nr.NativePublicRenderer(_flags(), runner=runner)
        self.assertEqual(renderer.render(PAGE_URL, max_bytes=10), b"A" * 10)

    def test_worker_error_is_surfaced_without_raw_stderr(self):
        def runner(payload, **kwargs):
            return json.dumps({"ok": False, "error": "render navigation failed"}).encode()

        renderer = nr.NativePublicRenderer(_flags(), runner=runner)
        with self.assertRaises(nr.RenderUnavailable) as caught:
            renderer.render(PAGE_URL)
        self.assertIn("navigation failed", str(caught.exception))

    def test_malformed_worker_result_is_unavailable(self):
        renderer = nr.NativePublicRenderer(
            _flags(), runner=lambda payload, **kwargs: b"not json")
        with self.assertRaises(nr.RenderUnavailable):
            renderer.render(PAGE_URL)

    def test_child_environment_is_allowlisted_and_secret_free(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            env = nr._child_env(tmp, environ={
                "PATH": "/bin", "PYTHONPATH": "/deps",
                "PLAYWRIGHT_BROWSERS_PATH": "/browsers",
                "OPENAI_API_KEY": "secret", "AWS_SECRET_ACCESS_KEY": "secret",
                "GITHUB_TOKEN": "secret", "HOME": "/owner",
            })
        self.assertEqual(env["HOME"], tmp)
        self.assertEqual(env["TMPDIR"], tmp)
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], "/browsers")
        for key in ("OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN"):
            self.assertNotIn(key, env)

    def test_terminate_owned_falls_back_and_reaps(self):
        class _Proc:
            pid = None

            def __init__(self):
                self.terminated = False
                self.killed = False
                self.waited = 0

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                self.waited += 1
                return 0

            def poll(self):
                return None

        proc = _Proc()
        nr._terminate_owned(proc)
        self.assertTrue(proc.terminated)
        self.assertGreaterEqual(proc.waited, 1)
        self.assertFalse(proc.killed)

    def test_group_cleanup_kills_owned_group_after_leader_already_exited(self):
        signals = []

        class _ExitedLeader:
            pid = 4242

            def wait(self, timeout=None):
                return 0

        def fake_killpg(pgid, sig):
            signals.append((pgid, sig))
            if sig == 0:
                # The group still has a stubborn descendant until SIGKILL.
                if any(item[1] == nr.signal.SIGKILL for item in signals):
                    raise ProcessLookupError()
                return None
            return None

        with mock.patch.object(nr.os, "getpgrp", return_value=1), \
                mock.patch.object(nr.os, "killpg", side_effect=fake_killpg):
            nr._terminate_owned(_ExitedLeader(), pgid=4242, grace=0.01)
        self.assertIn((4242, nr.signal.SIGTERM), signals)
        self.assertIn((4242, nr.signal.SIGKILL), signals)
        self.assertNotIn(1, [pgid for pgid, _ in signals],
                         "the parent process group must never be signalled")

    def test_own_process_group_is_never_signalled_as_a_group(self):
        class _Proc:
            pid = None

            def __init__(self):
                self.terminated = False

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

        proc = _Proc()
        with mock.patch.object(nr.os, "getpgrp", return_value=7), \
                mock.patch.object(nr.os, "killpg",
                                  side_effect=AssertionError("own group signalled")):
            nr._terminate_owned(proc, pgid=7, grace=0.01)
        self.assertTrue(proc.terminated)

    def test_subprocess_deadline_terminates_and_reaps_owned_worker(self):
        class _FakeStdin:
            closed = False

            def write(self, data):
                pass

            def close(self):
                self.closed = True

        class _FakePopen:
            pid = None
            returncode = None
            stdout = None
            stderr = None

            def __init__(self):
                self.stdin = _FakeStdin()
                self.terminated = False
                self.killed = False

            def wait(self, timeout=None):
                if self.terminated or self.killed:
                    self.returncode = 0
                    return 0
                raise nr.subprocess.TimeoutExpired("worker", timeout)

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        proc = _FakePopen()
        with mock.patch.object(nr.subprocess, "Popen", return_value=proc):
            with self.assertRaises(nr.RenderUnavailable) as caught:
                nr._run_worker_subprocess(
                    {"url": PAGE_URL, "timeout": 1, "max_output_bytes": 100},
                    timeout=1, tmpdir=str(ROOT), env={},
                    worker_path=Path("worker.py"), python_executable=sys.executable)
        self.assertIn("timed out", str(caught.exception))
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.stdin.closed)


class RoutedRetrievalIntegrationTests(unittest.TestCase):
    def test_old_url_only_injected_callable_still_works(self):
        calls = []

        def renderer(url):
            calls.append(url)
            return b"<html>ok</html>"

        data = rr.render_js_shell(PAGE_URL, renderer=renderer, policy=_flags())
        self.assertIn(b"ok", data)
        self.assertEqual(calls, [PAGE_URL])

    def test_object_renderer_receives_residual_timeout_and_limit(self):
        class _Renderer:
            def __init__(self):
                self.calls = []

            def render(self, url, *, timeout=None, max_bytes=None):
                self.calls.append((url, timeout, max_bytes))
                return b"<html>ok</html>"

        renderer = _Renderer()
        rr.render_js_shell(PAGE_URL, renderer=renderer, policy=_flags(),
                           timeout=2, max_bytes=1234)
        self.assertEqual(renderer.calls[0][0], PAGE_URL)
        self.assertEqual(renderer.calls[0][1], 2)
        self.assertEqual(renderer.calls[0][2], 1234)

    def test_capabilities_report_builtin_when_flags_on_without_injection(self):
        caps = rr.capabilities(_flags())
        self.assertTrue(caps["native_render_fallback"]["available"])
        self.assertIn("built-in", caps["native_render_fallback"]["reason"])

    def test_capabilities_remain_off_by_neutral_default(self):
        caps = rr.capabilities({})
        self.assertFalse(caps["native_render_fallback"]["available"])


if __name__ == "__main__":
    unittest.main()
