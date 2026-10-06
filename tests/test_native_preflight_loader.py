"""Synthetic tests for the corrected native-context bootstrap loader.

No Hermes runtime, no network, no model, no live profile, no credential. Every
host module the loader touches (``hermes_cli.plugins``, ``hermes_cli.plugins_loader``,
``hermes_constants``, ``tools.mcp_tool_discovery``) plus the ``secretary_cli``
context holder are stubbed in ``sys.modules`` and restored afterwards.

These tests pin the native bootstrap's ordering and dispatch checks:

* the bounded native bootstrap runs plugin discovery FIRST, then the supported
  ``discover_mcp_tools(allowed_mcp_names=[server])`` in the caller's unchanged
  call-time scope; plugin-home scope is compared separately, without pinning;
* a non-callable server raises ``NativeBootstrapError`` with the exact reason
  (including a scope mismatch that is only visible *outside* the home scope)
  instead of returning a context that yields the generic "not connected".
"""
from __future__ import annotations

import importlib.util
import sys
import types
import threading
import contextlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Load the candidate loader by file path (no sys.path pollution that could
# shadow an unrelated ``common``/``paths`` module for other test modules).
_spec = importlib.util.spec_from_file_location(
    "_candidate_parent_context_loader", ROOT / "finalization" / "parent_context_loader.py")
loader = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = loader
_spec.loader.exec_module(loader)

_MISSING = object()
_SENTINEL_CTX = types.SimpleNamespace(plugin_id="secretary",
                                     _mcp_allowlist=lambda _: ["firecrawl"])


class _FakeScope:
    """Records enter/exit and marks whether the home scope is active."""

    def __init__(self, state):
        self._state = state

    def __enter__(self):
        self._state["scope_active"] = True
        self._state["events"].append(("scope_enter", self._state["home"]))
        return self

    def __exit__(self, *exc):
        self._state["scope_active"] = False
        self._state["events"].append(("scope_exit", self._state["home"]))
        return False


class HostStub:
    """Install fake host modules; restore the originals on exit."""

    def __init__(self, *, inside_status, outside_status, discovered=("fc_scrape",)):
        self.state = {
            "events": [],
            "scope_active": False,
            "home": "/fake/home",
            "discover_calls": [],
            "forbidden_calls": [],
        }
        self._inside_status = inside_status
        self._outside_status = outside_status
        self._discovered = list(discovered)
        self._saved = {}

    # -- a manager that records discovery order ------------------------------
    @property
    def manager(self):
        state = self.state

        class Manager:
            home_path = "/fake/home"

            def discover_and_load(self):
                state["events"].append(("plugin_load", None))

        return Manager()

    def _mod(self, name, **attrs):
        module = types.ModuleType(name)
        module.__path__ = []  # namespace stub so ``from pkg.mod import x`` resolves
        for key, value in attrs.items():
            setattr(module, key, value)
        return module

    def _install(self, name, module):
        self._saved[name] = sys.modules.get(name, _MISSING)
        sys.modules[name] = module

    def __enter__(self):
        state = self.state

        def get_mcp_status(configured=None, **kwargs):
            return state["scope_active"] and self._inside_status or self._outside_status

        def discover_mcp_tools(allowed_mcp_names=None):
            state["discover_calls"].append(list(allowed_mcp_names or []))
            state["events"].append(("mcp_discover", allowed_mcp_names))
            return list(self._discovered)

        def forbidden(*_a, **_k):
            state["forbidden_calls"].append("forbidden")
            raise AssertionError("a parallel/legacy probe must never be used")

        class Core:
            _lock = threading.RLock()
            _MCP_AVAILABLE = True
            _MCP_SDK_IMPORT_ATTEMPTED = True
            _server_connecting = set()
            _server_connect_errors = {}

            @property
            def _servers(self):
                entries = self_entries()
                return {e["name"]: types.SimpleNamespace(session=object())
                        for e in entries if e["status"] == "connected"}

            @property
            def _lazy_server_configs(self):
                return {e["name"]: {} for e in self_entries() if e["status"] == "lazy"}

            def _mcp_registry_scope(self):
                return None

        def self_entries():
            return self._inside_status if state["scope_active"] else self._outside_status

        self._install("hermes_cli", self._mod("hermes_cli"))
        self._install("hermes_cli.plugins", self._mod(
            "hermes_cli.plugins", get_plugin_manager=lambda: self.manager))
        self._install("hermes_cli.plugins_loader", self._mod(
            "hermes_cli.plugins_loader", _plugin_home_scope=lambda home: _FakeScope(state)))
        self._install("hermes_constants", self._mod(
            "hermes_constants",
            process_hermes_home_is_pinned=lambda: True,
            pin_process_hermes_home=lambda home: None))
        self._install("tools", self._mod("tools"))
        self._install("tools.mcp_tool_discovery", self._mod(
            "tools.mcp_tool_discovery",
            discover_mcp_tools=discover_mcp_tools,
            get_mcp_status=get_mcp_status,
            _core=Core(), _resolve_server_key=lambda n: n, _key_name=lambda k: k,
            _connect_cooldown_active=lambda n: False,
            _owner_secret_scope=contextlib.nullcontext,
            _config=types.SimpleNamespace(_load_mcp_config=lambda: {"firecrawl": {}}),
            mcp_server_enabled=lambda cfg: cfg.get("enabled", True),
            _resolve_server_lazy=lambda n, cfg: cfg.get("lazy", False),
            probe_mcp_server_tools=forbidden,
            register_mcp_servers=forbidden))
        self._install("secretary_cli", self._mod(
            "secretary_cli", get_native_context=lambda: _SENTINEL_CTX))
        return self

    def __exit__(self, *exc):
        for name, old in self._saved.items():
            if old is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
        return False


def _status(name, status):
    return {"name": name, "status": status, "connected": status == "connected", "tools": 27}


class NativeBootstrapTests(unittest.TestCase):
    def test_plugin_discovery_runs_before_bounded_mcp_discovery(self):
        with HostStub(inside_status=[_status("firecrawl", "connected")],
                      outside_status=[_status("firecrawl", "connected")]) as stub:
            report = loader.bootstrap_report("firecrawl")
        events = [e[0] for e in stub.state["events"]]
        self.assertIn("plugin_load", events)
        self.assertIn("mcp_discover", events)
        self.assertLess(events.index("plugin_load"), events.index("mcp_discover"),
                        "plugin discovery (which captures the ctx / scope) must run first")
        self.assertTrue(report["ok"])

    def test_bounded_discovery_names_only_the_target_server(self):
        with HostStub(inside_status=[_status("firecrawl", "connected")],
                      outside_status=[_status("firecrawl", "connected")]) as stub:
            loader.bootstrap_report("firecrawl")
        self.assertEqual(stub.state["discover_calls"], [["firecrawl"]],
                         "discovery must be bounded to exactly the named server")

    def test_get_context_returns_captured_context_when_connected(self):
        with HostStub(inside_status=[_status("firecrawl", "connected")],
                      outside_status=[_status("firecrawl", "connected")]):
            ctx = loader.get_context("firecrawl")
        self.assertIs(ctx, _SENTINEL_CTX)

    def test_not_connected_raises_with_exact_reason(self):
        with HostStub(inside_status=[_status("firecrawl", "failed")],
                      outside_status=[_status("firecrawl", "failed")]):
            with self.assertRaises(loader.NativeBootstrapError) as caught:
                loader.get_context("firecrawl")
        self.assertEqual(str(caught.exception), "native_connect_failed_not_connected")
        self.assertEqual(caught.exception.details["server"], "firecrawl")
        self.assertFalse(caught.exception.details["scope_mismatch"])

    def test_scope_mismatch_is_reported_not_hidden(self):
        # Visible inside the home scope, but the call-time scope resolves nothing:
        # exactly the defect the original loader hid behind a generic message.
        with HostStub(inside_status=[_status("firecrawl", "connected")],
                      outside_status=[_status("firecrawl", "configured")]) as stub:
            with self.assertRaises(loader.NativeBootstrapError) as caught:
                loader.get_context("firecrawl")
        self.assertTrue(caught.exception.details["scope_mismatch"])
        self.assertIn("scope", str(caught.exception).lower())
        # The home scope was entered and left for the comparison only.
        events = [e[0] for e in stub.state["events"]]
        self.assertIn("scope_enter", events)
        self.assertIn("scope_exit", events)

    def test_lazy_registration_counts_as_dispatchable(self):
        with HostStub(inside_status=[_status("firecrawl", "lazy")],
                      outside_status=[_status("firecrawl", "lazy")]):
            report = loader.bootstrap_report("firecrawl")
        self.assertTrue(report["ok"])

    def test_report_is_structured_and_never_uses_a_parallel_client(self):
        with HostStub(inside_status=[_status("firecrawl", "connected")],
                      outside_status=[_status("firecrawl", "connected")]) as stub:
            report = loader.bootstrap_report("firecrawl")
        for key in ("ok", "server", "discovered_tools", "status_in_home_scope",
                    "status_at_call_scope", "scope_mismatch", "parallel_client"):
            self.assertIn(key, report)
        self.assertFalse(report["parallel_client"])
        self.assertFalse(report["grant_changes"])
        self.assertEqual(stub.state["forbidden_calls"], [])

    def test_un_capturable_context_fails_closed(self):
        with HostStub(inside_status=[], outside_status=[]) as stub:
            sys.modules["secretary_cli"].get_native_context = lambda: None
            with self.assertRaises(loader.NativeBootstrapError) as caught:
                loader.bootstrap_report("firecrawl")
        self.assertIn("not captured", str(caught.exception))

    def test_empty_effective_config_distinguished_from_sdk_failure(self):
        with HostStub(inside_status=[], outside_status=[], discovered=()):
            d = sys.modules["tools.mcp_tool_discovery"]
            d._config._load_mcp_config = lambda: {}
            report = loader.bootstrap_report()
        self.assertEqual(report["reason"], "effective_config_empty")
        self.assertTrue(report["discovery_empty"])

    def test_sdk_unavailable_and_name_filter_have_distinct_reasons(self):
        with HostStub(inside_status=[], outside_status=[], discovered=()):
            d = sys.modules["tools.mcp_tool_discovery"]
            d._core._MCP_AVAILABLE = False
            self.assertEqual(loader.bootstrap_report()["reason"], "sdk_unavailable")
            d._config._load_mcp_config = lambda: {"other": {}}
            self.assertEqual(loader.bootstrap_report()["reason"], "target_filtered_out")

    def test_no_home_pin_and_no_raw_error_or_status_values(self):
        with HostStub(inside_status=[], outside_status=[]) as stub:
            sys.modules["hermes_constants"].pin_process_hermes_home = lambda _: self.fail("must not pin")
            d = sys.modules["tools.mcp_tool_discovery"]
            d.get_mcp_status = lambda: [{"name": "firecrawl", "status": "failed",
                                        "error": "https://credential.invalid/SECRET", "sampling": {"SECRET": 1}}]
            def fail(**kwargs):
                print("SECRET")
                raise ValueError("https://credential.invalid/SECRET")
            d.discover_mcp_tools = fail
            report = loader.bootstrap_report()
        text = json.dumps(report)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("/fake/home", text)
        self.assertEqual(report["reason"], "native_discovery_exception")

    def test_status_visibility_does_not_override_exact_dispatch_key(self):
        with HostStub(inside_status=[_status("firecrawl", "connected")],
                      outside_status=[_status("firecrawl", "connected")]):
            d = sys.modules["tools.mcp_tool_discovery"]
            d._resolve_server_key = lambda n: ("foreign-scope", n)
            report = loader.bootstrap_report()
        self.assertFalse(report["ok"])
        self.assertFalse(report["status_at_call_scope"]["dispatchable"])

    def test_missing_grant_does_not_discover(self):
        with HostStub(inside_status=[], outside_status=[]) as stub:
            sys.modules["secretary_cli"].get_native_context = lambda: types.SimpleNamespace(
                plugin_id="secretary", _mcp_allowlist=lambda _: [])
            report = loader.bootstrap_report()
        self.assertEqual(report["reason"], "native_grant_missing")
        self.assertEqual(stub.state["discover_calls"], [])


if __name__ == "__main__":
    unittest.main()
