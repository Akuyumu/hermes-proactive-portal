"""Native search bridge fake tests: taxonomy, caching, budgets, redaction.

No subprocess, no network, no model: the runner and provider payloads are fake.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import searchbridge as sb  # noqa: E402
from proactive_pilot.publicfeeds import URLRejected, validate_public_url  # noqa: E402

# Faithful shape actually observed from ``hermes --print-runtime-command``: a
# native isolated interpreter whose ``-c`` bootstrap ends by dispatching
# hermes_cli.main. The old fake prefix ["hermes", "runtime"] was NOT faithful and
# hid an invalid argv, so every fake now uses the real shape.
BOOTSTRAP = (
    "import os, sys, runpy; "
    "os.environ.pop('PYTHONHOME', None); os.environ.pop('PYTHONPATH', None); "
    "os.environ.pop('VIRTUAL_ENV', None); "
    "sys.path.insert(0, '/native/hermes'); "
    "os.environ['HERMES_HOME'] = os.environ.get('HERMES_HOME') or "
    "str(__import__('hermes_constants').get_default_hermes_root()); "
    "import hermes_bootstrap; "
    "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
)
ACTUAL_PREFIX = ["/native/python", "-I", "-c", BOOTSTRAP]
_MISSING = object()


def _runtime_ok(*_args, **_kwargs):
    return 0, json.dumps(ACTUAL_PREFIX), ""


def _search_calls(runner):
    return [call for call in runner.calls if call and call[0] == "/native/python"]


def _execute_generated_entrypoint(code, extra_argv, tool_result):
    """Exec the generated native ``-c`` bootstrap in-process with stubs.

    No real interpreter, subprocess, socket, network, private file or native
    runtime is touched: ``tools.web_tools``, ``hermes_bootstrap`` and
    ``hermes_constants`` are stubbed in ``sys.modules`` and every mutated global
    is restored.
    """
    captured: dict = {}

    def fake_web_search_tool(query, limit=5):
        captured["query"] = query
        captured["limit"] = limit
        return tool_result

    tools_pkg = types.ModuleType("tools")
    web_tools = types.ModuleType("tools.web_tools")
    web_tools.web_search_tool = fake_web_search_tool
    tools_pkg.web_tools = web_tools
    bootstrap = types.ModuleType("hermes_bootstrap")
    constants = types.ModuleType("hermes_constants")
    constants.get_default_hermes_root = lambda: "/nonexistent/hermes-root"

    saved_modules = {}
    for name, module in (("tools", tools_pkg), ("tools.web_tools", web_tools),
                         ("hermes_bootstrap", bootstrap), ("hermes_constants", constants)):
        saved_modules[name] = sys.modules.get(name, _MISSING)
        sys.modules[name] = module
    saved_argv = sys.argv
    saved_env = dict(os.environ)
    saved_path = list(sys.path)
    buf = io.StringIO()
    os.environ["HERMES_HOME"] = "/nonexistent/hermes-home"
    sys.argv = ["-c"] + list(extra_argv)
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<native-bootstrap>", "exec"), {"__name__": "__main__"})
    finally:
        sys.argv = saved_argv
        sys.path[:] = saved_path
        os.environ.clear()
        os.environ.update(saved_env)
        for name, old in saved_modules.items():
            if old is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
    return captured, buf.getvalue()


def _payload(rows):
    return json.dumps({"success": True, "data": {"web": rows}})


def _row(url, title="t", description="d"):
    return {"title": title, "url": url, "description": description}


_DEFAULT = object()


class FakeRunner:
    """Records argv; returns a canned runtime prefix then a search payload."""

    def __init__(self, payloads=None, *, prefix=_DEFAULT, search_rc=0, search_err=""):
        self.payloads = list(payloads or [])
        self.prefix = json.dumps(ACTUAL_PREFIX) if prefix is _DEFAULT else prefix
        self.search_rc = search_rc
        self.search_err = search_err
        self.calls: list[list[str]] = []

    def run(self, argv, timeout):
        self.calls.append(list(argv))
        if "--print-runtime-command" in argv:
            return (0, self.prefix, "") if self.prefix is not None else (1, "", "no runtime")
        if self.payloads:
            return self.search_rc, self.payloads.pop(0), self.search_err
        return self.search_rc, "", self.search_err


def _bridge(runner, **policy):
    return sb.NativeSearchBridge(runner, policy=policy, clock=lambda: 1000.0)


class NativeSearchBridgeTests(unittest.TestCase):
    def test_runtime_prefix_and_successful_search(self):
        runner = FakeRunner([_payload([_row("https://example.org/feed.xml", "Feed")])])
        bridge = _bridge(runner)
        out = bridge.search("quantum networking")
        self.assertEqual(out.status, sb.STATUS_OK)
        self.assertEqual(len(out.hits), 1)
        self.assertEqual(out.hits[0].host, "example.org")
        # Fixed-function argv derived from the ACTUAL runtime prefix: the native
        # interpreter/isolated/``-c`` shape is preserved, only the known
        # hermes_cli.main dispatch is replaced by a static import. No invented
        # --run-module/--function/--args flags.
        search_argv = _search_calls(runner)[0]
        self.assertEqual(search_argv[:3], ["/native/python", "-I", "-c"])
        for flag in ("--run-module", "--function", "--args"):
            self.assertNotIn(flag, search_argv)
        code = search_argv[3]
        self.assertIn("from tools.web_tools import web_search_tool", code)
        self.assertIn("web_search_tool(payload['query'], limit=5)", code)
        self.assertIn("import hermes_bootstrap", code)
        self.assertIn("sys.path.insert(0, '/native/hermes')", code)
        self.assertNotIn(sb.BOOTSTRAP_DISPATCH, code)
        self.assertEqual(json.loads(search_argv[4]), {"query": "quantum networking"})

    def test_healthy_empty_is_not_failure(self):
        runner = FakeRunner([_payload([])])
        out = _bridge(runner).search("nothing here")
        self.assertEqual(out.status, sb.STATUS_EMPTY)
        self.assertTrue(out.healthy_empty)
        self.assertFalse(out.failed)
        self.assertEqual(out.hits, [])

    def test_unsupported_when_runtime_missing(self):
        runner = FakeRunner(prefix=None)
        out = _bridge(runner).search("x")
        self.assertEqual(out.status, sb.STATUS_UNSUPPORTED)
        self.assertTrue(out.failed)

    def test_rate_limited_sets_backoff_and_stops(self):
        runner = FakeRunner([json.dumps({"success": False, "error": "HTTP 429 too many requests"})])
        bridge = _bridge(runner)
        first = bridge.search("q1")
        self.assertEqual(first.status, sb.STATUS_RATE_LIMITED)
        before = len(_search_calls(runner))
        second = bridge.search("q2")
        self.assertEqual(second.status, sb.STATUS_RATE_LIMITED)
        after = len(_search_calls(runner))
        self.assertEqual(before, after, "rate limit must not trigger another search")

    def test_malformed_payload_is_invalid(self):
        runner = FakeRunner(["not json at all"])
        out = _bridge(runner).search("q")
        self.assertEqual(out.status, sb.STATUS_INVALID)
        self.assertTrue(out.failed)

    def test_non_public_urls_are_dropped_never_fabricated(self):
        runner = FakeRunner([_payload([
            _row("https://127.0.0.1/feed"),
            _row("http://example.org/insecure"),
            _row("https://user:pass@example.org/feed"),
        ])])
        out = _bridge(runner).search("q")
        self.assertEqual(out.status, sb.STATUS_EMPTY)
        self.assertEqual(out.raw_count, 3)
        self.assertEqual(out.hits, [])

    def test_provider_error_text_is_never_exposed(self):
        runner = FakeRunner([json.dumps({"success": False, "error": "token=SECRET123 leaked"})])
        out = _bridge(runner).search("q")
        self.assertEqual(out.status, sb.STATUS_FAILED)
        self.assertNotIn("SECRET123", out.error)
        self.assertNotIn("token", out.error.lower())

    def test_per_cycle_budget_is_finite(self):
        runner = FakeRunner([_payload([]), _payload([])])
        bridge = _bridge(runner, native_search_max_queries_per_cycle=1)
        self.assertEqual(bridge.search("a").status, sb.STATUS_EMPTY)
        out = bridge.search("b")
        self.assertEqual(out.status, sb.STATUS_FAILED)
        self.assertIn("budget", out.error)

    def test_warm_cache_avoids_second_call(self):
        runner = FakeRunner([_payload([_row("https://example.org/a", "A")])])
        bridge = _bridge(runner)
        bridge.search("same")
        calls_after_cold = len(_search_calls(runner))
        warm = bridge.search("same")
        calls_after_warm = len(_search_calls(runner))
        self.assertTrue(warm.from_cache)
        self.assertEqual(calls_after_cold, calls_after_warm)

    def test_url_validator_is_applied_to_hits(self):
        def deny(url):
            raise URLRejected("nope")
        runner = FakeRunner([_payload([_row("https://example.org/a")])])
        bridge = sb.NativeSearchBridge(runner, policy={}, url_validator=deny)
        out = bridge.search("q")
        self.assertEqual(out.status, sb.STATUS_EMPTY)

    def test_publicfeeds_validator_rejects_secret_query(self):
        self.assertRaises(URLRejected, validate_public_url,
                          "https://example.org/feed?access_token=x")


class NativeContractFaithfulTests(unittest.TestCase):
    """Executes the generated entrypoint against stubs to prove the real shape."""

    def test_generated_entrypoint_roundtrips_quotes_unicode_and_newline(self):
        raw = "caf\u00e9 \"quantum\"\nresearch\t\u00fcn\u00efcode"
        runner = FakeRunner([])
        bridge = _bridge(runner)
        argv = bridge.build_argv(raw, ACTUAL_PREFIX)
        payload = json.dumps(
            {"success": True, "data": {"web": [{"title": "T", "url": "https://example.org/x"}]}},
            ensure_ascii=False)
        captured, printed = _execute_generated_entrypoint(argv[3], argv[4:], payload)
        self.assertEqual(captured["query"], raw, "query must round-trip verbatim")
        self.assertEqual(captured["limit"], 5, "fixed limit is 5")
        self.assertEqual(json.loads(printed.strip()), json.loads(payload))
        self.assertIsInstance(printed, str)

    def test_static_fixed_function_is_called_not_a_dynamic_dispatch(self):
        runner = FakeRunner([])
        bridge = _bridge(runner)
        argv = bridge.build_argv("q", ACTUAL_PREFIX)
        code = argv[3]
        # The replacement is a static import of the fixed function, not a
        # dynamic/reflective dispatch. (The native setup's own
        # ``__import__('hermes_constants')`` is preserved verbatim.)
        self.assertEqual(code.count("from tools.web_tools import web_search_tool"), 1)
        self.assertNotIn("runpy.run_module('hermes_cli.main'", code)
        self.assertNotIn("importlib", code)
        self.assertNotIn("subprocess", code)
        self.assertNotIn("--run-module", argv)
        self.assertNotIn("--function", argv)
        captured, _printed = _execute_generated_entrypoint(
            code, argv[4:], json.dumps({"success": True, "data": {"web": []}}))
        self.assertEqual(captured.get("query"), "q")

    def test_arbitrary_module_or_function_is_rejected_fail_closed(self):
        for module, function in (("os", "system"), ("tools.web_tools", "web_search"),
                                 ("evil.module", "web_search_tool")):
            runner = FakeRunner([_payload([])])
            bridge = sb.NativeSearchBridge(runner, policy={}, module=module,
                                           function=function, clock=lambda: 1000.0)
            out = bridge.search("q")
            self.assertEqual(out.status, sb.STATUS_UNSUPPORTED, (module, function))
            self.assertEqual(_search_calls(runner), [],
                             "no arbitrary module/function may be launched")

    def test_malformed_prefix_fails_closed(self):
        runner = FakeRunner([_payload([])])
        bridge = _bridge(runner)
        for bad in (
            [],  # no -c at all
            ["/usr/bin/hermes", "--print-runtime-command"],  # old invented shape
            ["/native/python", "-c", "print('hello')"],  # not the native dispatch
            ["/native/python", "-I", "-c",
             sb.BOOTSTRAP_DISPATCH + "; " + sb.BOOTSTRAP_DISPATCH],  # ambiguous
        ):
            with self.assertRaises(sb.BridgeError):
                bridge.build_argv("q", bad)

    def test_unparseable_prefix_output_fails_closed(self):
        runner = FakeRunner([], prefix="not-json-")
        out = _bridge(runner).search("q")
        self.assertEqual(out.status, sb.STATUS_UNSUPPORTED)
        self.assertEqual(_search_calls(runner), [])


if __name__ == "__main__":
    unittest.main()
