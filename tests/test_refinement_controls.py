"""Refinement regression controls (H1-H3, M1-M4, L1-L4) + fake perf suite.

Every test is offline: fake runners, fake connectors, fake DNS tables and a
workspace temp dir. No subprocess, no socket, no network, no model.
"""
from __future__ import annotations

import json
import socket
import ssl
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import publictransport as pt  # noqa: E402
from proactive_pilot import searchbridge as sb  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import Budget  # noqa: E402
from proactive_pilot.discovery import discover_public_sources  # noqa: E402
from proactive_pilot.intake import (  # noqa: E402
    IntakeError,
    derive_deterministic_topic,
    intake_to_action,
    looks_like_explicit_request,
)
from proactive_pilot.main import collect_public_and_adaptive  # noqa: E402
from proactive_pilot.planner import (  # noqa: E402
    FakePlanner,
    PlannerInvalid,
    plan_for_watch,
)
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.publicfeeds import (  # noqa: E402
    FetchResponse,
    WebPageUnavailable,
    classify_web_page,
    content_type_allowed,
    parse_web_page,
    sanitize_evidence_link,
)
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.watchstore import apply_action  # noqa: E402

NOW = "2026-10-03T12:00:00"

# Faithful native runtime prefix currently returned by
# ``hermes --print-runtime-command`` (bootstrap dispatching hermes_cli.main).
_PREFIX = [
    "/native/python", "-I", "-c",
    "import os, sys, runpy; os.environ.pop('PYTHONHOME', None); "
    "os.environ.pop('PYTHONPATH', None); os.environ.pop('VIRTUAL_ENV', None); "
    "sys.path.insert(0, '/native/hermes'); "
    "os.environ['HERMES_HOME'] = os.environ.get('HERMES_HOME') or "
    "str(__import__('hermes_constants').get_default_hermes_root()); "
    "import hermes_bootstrap; "
    "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)",
]


def _payload(urls):
    return {"success": True, "data": {"web": [
        {"title": f"T{i}", "url": u, "description": "d"} for i, u in enumerate(urls)]}}


def _resolver(table):
    def resolve(host):
        if host not in table:
            raise pt.TransportRejected("no fake dns record")
        return table[host]
    return resolve


# --------------------------------------------------------------------------- #
# L4 + H3: global-IP policy and pinned transport
# --------------------------------------------------------------------------- #


class PinnedTransportTests(unittest.TestCase):
    def test_ip_is_global_rejects_non_public_and_cgnat(self):
        for good in ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"):
            self.assertTrue(pt.ip_is_global(good), good)
        for bad in ("10.0.0.1", "127.0.0.1", "169.254.169.254", "0.0.0.0",
                    "::1", "::ffff:127.0.0.1", "100.64.0.1", "198.18.0.1",
                    "192.0.2.5", "not-an-ip"):
            self.assertFalse(pt.ip_is_global(bad), bad)

    def test_ip_is_global_rejects_every_ipv4_mapped_form(self):
        # A mapped IPv6 address is never accepted, even when it embeds a public
        # IPv4: the doc and behaviour agree that mapped forms are refused.
        for mapped in ("::ffff:93.184.216.34", "::ffff:8.8.8.8",
                       "::ffff:127.0.0.1", "::ffff:10.0.0.1"):
            self.assertFalse(pt.ip_is_global(mapped), mapped)

    def test_all_resolved_ips_must_be_public(self):
        with self.assertRaises(pt.TransportRejected):
            pt.resolve_public_ips("news.example",
                                  resolver=_resolver({"news.example": ["10.0.0.1"]}))
        with self.assertRaises(pt.TransportRejected):
            pt.resolve_public_ips("news.example", resolver=_resolver(
                {"news.example": ["93.184.216.34", "127.0.0.1"]}))
        self.assertEqual(
            pt.resolve_public_ips("news.example",
                                  resolver=_resolver({"news.example": ["93.184.216.34"]})),
            ["93.184.216.34"])

    class _Resp:
        def __init__(self, status, headers, body=b""):
            self.status = status
            self.headers = headers
            self._body = body

        def read(self, _n=-1):
            return self._body

    class _Conn:
        def __init__(self, record, status, headers, body):
            self.record = record
            self.status = status
            self.headers = headers
            self.body = body

        def request(self, method, path, headers=None):
            self.record.append({"method": method, "path": path})

        def getresponse(self):
            return PinnedTransportTests._Resp(self.status, self.headers, self.body)

        def close(self):
            pass

    def test_connect_is_pinned_to_validated_ip(self):
        connects = []
        calls = []

        def connector(host, port, ip, timeout):
            connects.append((host, port, ip))
            return PinnedTransportTests._Conn(calls, 200, {"Content-Type": "text/html"}, b"<title>x</title>")

        transport = pt.PinnedHTTPSTransport(
            resolver=_resolver({"news.example": ["93.184.216.34"]}),
            connector=connector)
        response = transport.fetch("https://news.example/story")
        self.assertEqual(response.status, 200)
        # The socket would be opened to the PINNED ip while the real hostname is
        # retained for Host/TLS validation.
        self.assertEqual(connects, [("news.example", 443, "93.184.216.34")])
        self.assertEqual(calls[0]["path"], "/story")

    def test_redirect_hop_is_revalidated_and_rejected(self):
        connects = []

        def connector(host, port, ip, timeout):
            connects.append(ip)
            return PinnedTransportTests._Conn([], 302, {"Location": "https://127.0.0.1/x"}, b"")

        transport = pt.PinnedHTTPSTransport(
            resolver=_resolver({"news.example": ["93.184.216.34"]}),
            connector=connector)
        with self.assertRaises(pt.TransportRejected):
            transport.fetch("https://news.example/story")
        # Only the first (public) hop connected; the private redirect target was
        # rejected before any connect.
        self.assertEqual(connects, ["93.184.216.34"])

    class _FakeSocket:
        """Minimal raw socket stand-in (no fd, no I/O)."""

        def setsockopt(self, *args, **kwargs):
            pass

        def settimeout(self, *args, **kwargs):
            pass

        def close(self):
            pass

    def test_initialized_connection_pins_ip_and_keeps_original_sni(self):
        # Exercise the REAL HTTPSConnection constructor + connect() path (the
        # fake-connector tests never did). ``socket.create_connection`` and the
        # TLS context are faked, so no socket is opened and no DNS is resolved.
        recorded = {}
        fake_raw = self._FakeSocket()
        fake_tls = object()

        class _FakeContext:
            check_hostname = True
            verify_mode = ssl.CERT_REQUIRED

            def wrap_socket(self, sock, server_hostname=None):
                recorded["tls_sock"] = sock
                recorded["server_hostname"] = server_hostname
                return fake_tls

        def fake_create_connection(address, timeout=None, source_address=None):
            recorded["address"] = address
            recorded["timeout"] = timeout
            return fake_raw

        def fake_getaddrinfo(*args, **kwargs):  # pragma: no cover - must not run
            recorded["dns_lookup"] = True
            raise AssertionError("the pinned connect must not re-resolve the host")

        original_cc = socket.create_connection
        original_gai = socket.getaddrinfo
        socket.create_connection = fake_create_connection
        socket.getaddrinfo = fake_getaddrinfo
        try:
            conn = pt._PinnedHTTPSConnection(
                "news.example", port=443, timeout=7, context=_FakeContext(),
                pin_ip="93.184.216.34")
            # The pin must survive HTTPConnection.__init__ (which otherwise
            # installs socket.create_connection over the class method). Bound
            # methods compare equal by (__self__, __func__).
            self.assertEqual(conn._create_connection, conn._connect_pinned)
            self.assertEqual(conn._create_connection.__func__,
                             conn._connect_pinned.__func__)
            conn.connect()
        finally:
            socket.create_connection = original_cc
            socket.getaddrinfo = original_gai
        self.assertEqual(recorded["address"][0], "93.184.216.34")
        self.assertNotEqual(recorded["address"][0], "news.example")
        self.assertEqual(recorded["address"][1], 443)
        self.assertEqual(recorded["server_hostname"], "news.example")
        self.assertIs(recorded["tls_sock"], fake_raw)
        self.assertIs(conn.sock, fake_tls)
        self.assertNotIn("dns_lookup", recorded)

    def test_default_connector_uses_default_context_and_pinned_connection(self):
        recorded = {}
        fake_tls = object()
        fake_raw = self._FakeSocket()

        class _FakeContext:
            check_hostname = True
            verify_mode = ssl.CERT_REQUIRED

            def wrap_socket(self, sock, server_hostname=None):
                recorded["server_hostname"] = server_hostname
                return fake_tls

        def fake_default_context():
            recorded["default_context"] = True
            return _FakeContext()

        def fake_create_connection(address, timeout=None, source_address=None):
            recorded["address"] = address
            return fake_raw

        original_cc = socket.create_connection
        original_ctx = ssl.create_default_context
        socket.create_connection = fake_create_connection
        ssl.create_default_context = fake_default_context
        try:
            transport = pt.PinnedHTTPSTransport()
            conn = transport._default_connector("news.example", 443, "93.184.216.34", 5)
        finally:
            socket.create_connection = original_cc
            ssl.create_default_context = original_ctx
        self.assertTrue(recorded.get("default_context"),
                        "production connector must build a default TLS context")
        self.assertEqual(recorded["address"][0], "93.184.216.34")
        self.assertEqual(recorded["server_hostname"], "news.example")
        self.assertIs(conn.sock, fake_tls)

    def test_default_tls_context_validates_hostnames(self):
        ctx = ssl.create_default_context()
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)


# --------------------------------------------------------------------------- #
# H2: persistent cross-process search cache keyed to watch revision
# --------------------------------------------------------------------------- #


class _Runner:
    def __init__(self, rows):
        self.rows = list(rows)
        self.search_calls = 0
        # Honest zero counters for the fake perf report: this adapter issues no
        # model turn and no HTTP request of its own.
        self.model_calls = 0
        self.http_calls = 0

    def run(self, argv, timeout):
        if "--print-runtime-command" in argv:
            return 0, json.dumps(_PREFIX), ""
        self.search_calls += 1
        return 0, json.dumps({"success": True, "data": {"web": self.rows.pop(0)}}), ""


class PersistentSearchCacheTests(unittest.TestCase):
    def test_unchanged_watch_makes_zero_extra_calls_across_bridge_objects(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW))
            runner1 = _Runner([[{"title": "A", "url": "https://example.org/a"}]])

            def wall():
                return 1000.0

            bridge1 = sb.build_native_bridge(
                {}, store=store, watch_id="w1", watch_version=3,
                runner=runner1, wall_clock=wall)
            first = bridge1.search("quantum networking")
            self.assertEqual(first.status, sb.STATUS_OK)
            self.assertEqual(runner1.search_calls, 1)
            # A brand new bridge/process object (same watch revision) reuses the
            # persisted outcome: zero extra backend calls until review cadence.
            runner2 = _Runner([[{"title": "B", "url": "https://example.org/b"}]])
            bridge2 = sb.build_native_bridge(
                {}, store=store, watch_id="w1", watch_version=3,
                runner=runner2, wall_clock=wall)
            second = bridge2.search("quantum networking")
            self.assertTrue(second.from_cache)
            self.assertEqual(len(second.hits), 1)
            self.assertEqual(second.hits[0].title, "A")
            self.assertEqual(runner2.search_calls, 0)
            # A changed watch revision invalidates the cache and re-queries.
            bridge3 = sb.build_native_bridge(
                {}, store=store, watch_id="w1", watch_version=4,
                runner=_Runner([[{"title": "C", "url": "https://example.org/c"}]]),
                wall_clock=wall)
            third = bridge3.search("renewable energy")
            self.assertEqual(third.status, sb.STATUS_OK)

    def test_stop_forget_purges_persisted_cache(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW))
            runner = _Runner([[{"title": "A", "url": "https://example.org/a"}]])
            bridge = sb.build_native_bridge({}, store=store, watch_id="w1",
                                            watch_version=1, runner=runner,
                                            wall_clock=lambda: 1000.0)
            bridge.search("quantum networking")
            self.assertEqual(sb.purge_cached_watch(store, "w1"), 1)
            runner2 = _Runner([[{"title": "B", "url": "https://example.org/b"}]])
            bridge2 = sb.build_native_bridge({}, store=store, watch_id="w1",
                                             watch_version=1, runner=runner2,
                                             wall_clock=lambda: 1000.0)
            result = bridge2.search("quantum networking")
            self.assertFalse(result.from_cache)
            self.assertEqual(runner2.search_calls, 1)

    def test_persisted_429_backoff_survives_new_bridge(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW))
            wall = lambda: 1000.0  # noqa: E731

            class R:
                def __init__(self):
                    self.calls = 0

                def run(self, argv, timeout):
                    if "--print-runtime-command" in argv:
                        return 0, json.dumps(_PREFIX), ""
                    self.calls += 1
                    return 0, json.dumps({"success": False, "error": "429 too many"}), ""

            r1 = R()
            b1 = sb.build_native_bridge({}, store=store, watch_id="w1", watch_version=1,
                                        runner=r1, wall_clock=wall)
            self.assertEqual(b1.search("q").status, sb.STATUS_RATE_LIMITED)
            r2 = R()
            b2 = sb.build_native_bridge({}, store=store, watch_id="w1", watch_version=1,
                                        runner=r2, wall_clock=wall)
            self.assertEqual(b2.search("q2").status, sb.STATUS_RATE_LIMITED)
            self.assertEqual(r2.calls, 0, "persisted backoff must stop a new process")


# --------------------------------------------------------------------------- #
# H1: production wiring constructs a real bridge (both main and shadow)
# --------------------------------------------------------------------------- #


class _RecordingFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        for key, value in self.responses.items():
            if url.startswith(key):
                return value
        raise AssertionError(f"unexpected fetch {url}")


class WiringTests(unittest.TestCase):
    def test_main_constructs_native_bridge_when_flag_enabled(self):
        import proactive_pilot.searchbridge as sbmod

        recorded = {}

        def fake_build(policy, **kwargs):
            recorded.update(kwargs)
            return sb.FakeSearchBridge({})

        original = sbmod.build_native_bridge
        sbmod.build_native_bridge = fake_build
        try:
            with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
                clock = SeededClock(NOW)
                store = StateStore(Path(tmp), clock)
                apply_action(store, {"action": "add", "public_topic": "quantum networking",
                                     "scope": "reddit", "provenance": "explicit"}, clock.now())
                policy = json.loads(json.dumps(DEFAULT_POLICY))
                policy["adaptive_mode"] = "active"
                policy["adaptive_broad_discovery_enabled"] = True
                policy["discovery_topics_cap"] = 0
                policy["feed_seed_sources"] = []
                fetcher = _RecordingFetcher({})
                collect_public_and_adaptive(
                    policy, clock, store=store, context=[],
                    public_budget=Budget(5), planner=FakePlanner(
                        {"schema": 1, "queries": ["quantum networking"], "scope": "reddit"}),
                    fetcher=fetcher, public_fetcher=fetcher)
        finally:
            sbmod.build_native_bridge = original
        self.assertTrue(recorded, "main must build a native bridge when the flag is on")
        self.assertIn("watch_id", recorded)
        self.assertIn("watch_version", recorded)

    def test_build_native_bridge_returns_real_adapter(self):
        bridge = sb.build_native_bridge({}, runner=_Runner([[]]))
        self.assertIsInstance(bridge, sb.NativeSearchBridge)


# --------------------------------------------------------------------------- #
# M1 + M3: content-type gate and shared Reddit allowance
# --------------------------------------------------------------------------- #


class ContentTypeTests(unittest.TestCase):
    def test_content_type_route_matching(self):
        self.assertFalse(content_type_allowed("web_json", "text/html"))
        self.assertFalse(content_type_allowed("web_json", "application/octet-stream"))
        self.assertTrue(content_type_allowed("web_json", "application/json"))
        self.assertTrue(content_type_allowed("web_page", "text/html; charset=utf-8"))
        self.assertTrue(content_type_allowed("rss", "application/rss+xml"))
        self.assertTrue(content_type_allowed("web_json", ""))  # omit -> parser fails closed

    def test_discovered_html_as_json_fails_closed(self):
        from proactive_pilot import publicfeeds as pf
        clock = SeededClock(NOW)
        rec = {"id": "s1", "kind": "web_json", "url": "https://news.example/data.json",
               "host": "news.example", "discovered": True, "vetted_public": True,
               "last_fetch_at": "", "cache_at": "", "cache_items": []}

        def fetcher(url, **kwargs):
            return FetchResponse(200, {"Content-Type": "text/html"}, b"<html>nope</html>", url)

        result, _used = pf._collect_one(
            rec, policy={"feed_timeout_seconds": 5, "feed_max_bytes": 1000},
            clock=clock, now=clock.now(), fetcher=fetcher, reddit_budget=1)
        self.assertEqual(result.status, "error")
        self.assertIn("content-type", result.error)


class RedditLedgerTests(unittest.TestCase):
    def test_shared_ledger_allows_only_one_reddit_request_per_cycle(self):
        from proactive_pilot import publicfeeds as pf
        clock = SeededClock(NOW)
        fetched = []

        def fetcher(url, **kwargs):
            fetched.append(url)
            return FetchResponse(200, {}, b"<rss></rss>", url)

        ledger = pf.RedditLedger(1)
        rec = {"id": "r1", "kind": "reddit_rss", "url": "https://www.reddit.com/r/x/.rss",
               "host": "www.reddit.com", "last_fetch_at": ""}
        first, used1 = pf._collect_one(rec, policy={}, clock=clock, now=clock.now(),
                                       fetcher=fetcher, reddit_budget=1, reddit_ledger=ledger)
        rec2 = dict(rec, id="r2", url="https://www.reddit.com/r/y/.rss")
        second, used2 = pf._collect_one(rec2, policy={}, clock=clock, now=clock.now(),
                                        fetcher=fetcher, reddit_budget=1, reddit_ledger=ledger)
        self.assertEqual(len(fetched), 1, "only ONE Reddit request per shared cycle")
        self.assertEqual(used1, 1)
        self.assertEqual(used2, 0)


# --------------------------------------------------------------------------- #
# M2 + L2: honest aggregate discovery status
# --------------------------------------------------------------------------- #


class _SeqBridge:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append(query)
        return self.outcomes.pop(0)

    def search_signature_has_timeout(self, timeout=None):  # pragma: no cover
        return timeout


class DiscoveryAggregateTests(unittest.TestCase):
    def test_partial_when_some_queries_fail_but_sources_found(self):
        good = sb.outcome_from_payload("q1", _payload(["https://news.example/a"]), 0)
        bad = sb.SearchOutcome(query="q2", status=sb.STATUS_FAILED)
        bridge = _SeqBridge([good, bad])
        plan = {"watch_id": "w1", "watch_version": 1, "scope": "public",
                "queries": ["q1", "q2"]}
        policy = {"adaptive_broad_discovery_enabled": True,
                  "native_search_max_discovered_sources": 3}
        sources, summary = discover_public_sources(
            plan, policy, bridge, resolver=_resolver({"news.example": ["93.184.216.34"]}))
        self.assertEqual(len(sources), 1)
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["attempts"], {"ok": 1, "empty": 0, "failed": 1})

    def test_all_failed_is_not_ok_or_healthy_empty(self):
        bridge = _SeqBridge([sb.SearchOutcome(query="q", status=sb.STATUS_FAILED)])
        plan = {"watch_id": "w1", "watch_version": 1, "scope": "public", "queries": ["q"]}
        _sources, summary = discover_public_sources(
            plan, {"adaptive_broad_discovery_enabled": True}, bridge,
            resolver=_resolver({}))
        self.assertEqual(summary["status"], "failed")


# --------------------------------------------------------------------------- #
# M4 + L1: intake grammar, full-topic preservation, corrections, fallback
# --------------------------------------------------------------------------- #


class IntakeGrammarTests(unittest.TestCase):
    def test_non_requests_are_not_explicit(self):
        for text in ("watchdog", "my monitor broke", "I'll follow up",
                     "track my order", "keep an eye on my inbox",
                     "notify me about my account", "thanks", "ok", ""):
            self.assertFalse(looks_like_explicit_request(text), text)

    def test_real_requests_are_explicit(self):
        for text in ("watch quantum networking", "monitor mycorrhizal networks",
                     "keep me posted on robotics", "track renewable energy",
                     "alert me about scholarships"):
            self.assertTrue(looks_like_explicit_request(text), text)

    def test_full_topic_preserved_not_truncated(self):
        self.assertEqual(derive_deterministic_topic("watch quantum networking"),
                         "quantum networking")
        self.assertEqual(
            derive_deterministic_topic("watch research for beginners"),
            "research for beginners")

    def test_corrections_resolve_or_fail_closed(self):
        watches = [{"id": "w1", "public_topic": "quantum networking"},
                   {"id": "w2", "public_topic": "renewable energy"}]
        stopped = intake_to_action("stop watching quantum networking", policy={},
                                   now=SeededClock(NOW).now(), watches=watches)
        self.assertEqual((stopped["action"], stopped["id"]), ("stop", "w1"))
        snoozed = intake_to_action("snooze renewable energy for 2 hours", policy={},
                                   now=SeededClock(NOW).now(), watches=watches)
        self.assertEqual((snoozed["action"], snoozed["id"], snoozed["minutes"]),
                         ("snooze", "w2", 120))
        excluded = intake_to_action("exclude fundraising from quantum networking",
                                    policy={}, now=SeededClock(NOW).now(), watches=watches)
        self.assertEqual((excluded["action"], excluded["id"], excluded["exclude"]),
                         ("change", "w1", ["fundraising"]))
        with self.assertRaises(IntakeError):
            intake_to_action("stop", policy={}, now=SeededClock(NOW).now(), watches=watches)

    def test_model_invalid_plan_is_rejected_not_fallback(self):
        class Bad:
            def plan(self, watch, policy):
                raise PlannerInvalid("adversarial")

        watch = {"id": "w1", "version": 1, "public_topic": "quantum",
                 "scope": "public", "include": [], "exclude": []}
        plan, used = plan_for_watch(watch, DEFAULT_POLICY, Bad(), now=SeededClock(NOW).now())
        self.assertFalse(used)
        self.assertEqual(plan["provenance"], "model-invalid-rejected")
        self.assertEqual(plan["queries"], [])

    def test_unavailable_planner_uses_honest_deterministic_fallback(self):
        class Down:
            def plan(self, watch, policy):
                raise RuntimeError("model down")

        watch = {"id": "w1", "version": 1, "public_topic": "quantum networking",
                 "scope": "public", "include": [], "exclude": []}
        plan, used = plan_for_watch(watch, DEFAULT_POLICY, Down(), now=SeededClock(NOW).now())
        self.assertFalse(used)
        self.assertEqual(plan["provenance"], "deterministic-nonsemantic")


# --------------------------------------------------------------------------- #
# L3: untrusted evidence-link sanitization
# --------------------------------------------------------------------------- #


class EvidenceLinkTests(unittest.TestCase):
    def test_dangerous_and_huge_links_are_dropped(self):
        self.assertEqual(sanitize_evidence_link("javascript:alert(1)"), "")
        self.assertEqual(sanitize_evidence_link("data:text/html,x"), "")
        self.assertEqual(sanitize_evidence_link("x" * 5000), "")
        self.assertEqual(sanitize_evidence_link("https://user:pw@x.example/a"), "")
        self.assertEqual(sanitize_evidence_link("https://x.example/a?access_token=x"), "")
        self.assertEqual(sanitize_evidence_link("https://x.example/a"), "https://x.example/a")

    def test_parse_web_page_drops_dangerous_href(self):
        body = (b"<html><head><title>T</title></head><body>"
                b"<a href=\"javascript:alert(1)\">evil</a>"
                b"<a href=\"/safe\">safe link</a></body></html>")
        rows = parse_web_page(body)
        links = {row["link"] for row in rows}
        self.assertNotIn("javascript:alert(1)", links)
        self.assertIn("/safe", links)


# --------------------------------------------------------------------------- #
# HTTP 200 anti-bot / challenge content health (must never become a candidate)
# --------------------------------------------------------------------------- #


INCAPSULA_WAF = (
    b"<html><head><title>Request unsuccessful. Incapsula incident ID: "
    b"1234-5678</title></head><body>"
    b"<iframe src=\"/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e&ns=1\"></iframe>"
    b"<div>Request unsuccessful. Incapsula incident ID: 1234-5678</div>"
    b"</body></html>"
)
CLOUDFLARE_CHALLENGE = (
    b"<html><head><title>Just a moment...</title></head><body>"
    b"<div>Checking your browser before accessing example.org.</div>"
    b"<div>Enable JavaScript and cookies to continue</div>"
    b"</body></html>"
)
CAPTCHA_PAGE = (b"<html><head><title>Security check</title></head>"
                b"<body>Please complete the captcha to proceed</body></html>")
LOGIN_PAGE = (b"<html><head><title>Members</title></head>"
              b"<body><h1>Sign in to continue</h1></body></html>")
DYNAMIC_PAGE = (b"<html><head><title>App</title></head>"
                b"<body>JavaScript is required to view this page</body></html>")
ERROR_PAGE = b"<html><head><title>Oops</title></head><body>404 Not Found</body></html>"


class ContentHealthTests(unittest.TestCase):
    def test_incapsula_waf_skeleton_is_blocked_and_never_a_candidate(self):
        self.assertEqual(classify_web_page(INCAPSULA_WAF), "blocked")
        with self.assertRaises(WebPageUnavailable) as ctx:
            parse_web_page(INCAPSULA_WAF)
        self.assertEqual(ctx.exception.classification, "blocked")

    def test_cloudflare_captcha_login_classified_blocked(self):
        for body in (CLOUDFLARE_CHALLENGE, CAPTCHA_PAGE, LOGIN_PAGE):
            self.assertEqual(classify_web_page(body), "blocked", body[:40])
            with self.assertRaises(WebPageUnavailable):
                parse_web_page(body)

    def test_dynamic_and_error_skeletons_are_distinct(self):
        self.assertEqual(classify_web_page(DYNAMIC_PAGE), "dynamic")
        self.assertEqual(classify_web_page(ERROR_PAGE), "failure")
        self.assertEqual(classify_web_page(b""), "failure")
        self.assertEqual(classify_web_page(b"<html><body>x</body></html>"), "")

    def test_noindex_page_is_not_blanket_rejected(self):
        body = (b"<html><head><title>Legit Article</title>"
                b"<meta name=\"robots\" content=\"noindex, nofollow\">"
                b"<meta name=\"description\" content=\"real content\"></head>"
                b"<body><a href=\"/story\">Read the story</a></body></html>")
        self.assertEqual(classify_web_page(body), "")
        rows = parse_web_page(body)
        self.assertTrue(rows)

    def test_http200_challenge_does_not_become_a_healthy_source(self):
        from proactive_pilot import publicfeeds as pf
        clock = SeededClock(NOW)
        rec = {"id": "s1", "kind": "web_page", "url": "https://news.example/story",
               "host": "news.example", "discovered": True, "vetted_public": True,
               "last_fetch_at": "", "cache_at": "", "cache_items": []}

        def fetcher(url, **kwargs):
            return FetchResponse(200, {"Content-Type": "text/html"}, INCAPSULA_WAF, url)

        result, _used = pf._collect_one(
            rec, policy={"feed_timeout_seconds": 5, "feed_max_bytes": 10000},
            clock=clock, now=clock.now(), fetcher=fetcher, reddit_budget=1)
        self.assertEqual(result.status, "error")
        self.assertIn("blocked", result.error)
        self.assertNotEqual(result.status, "ok")
        self.assertEqual(result.items, [])


# --------------------------------------------------------------------------- #
# Fake deterministic cold/warm performance suite
# --------------------------------------------------------------------------- #


def run_perf_suite() -> dict:
    """Deterministic cold vs warm measurement with a fake finite-time runner.

    Cold = first bridge/process (one backend call). Warm = a NEW bridge and
    process object for the same watch revision (zero backend calls, served from
    the persisted cache). Both use a fake clock so results are reproducible.

    This is deliberately NOT a network or model benchmark: the runner is fake,
    model and HTTP counters are honest zeros, and only mechanical elapsed time
    and call counts are reported.
    """
    report = {
        "note": "deterministic offline fake measurement; NOT a network/model benchmark",
        "clock": "fake finite monotonic (no sleeps, no wall-clock network)",
        "cold": {}, "warm": {},
    }
    with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW))
        clock_ticks = {"t": 1000.0}

        def wall():
            clock_ticks["t"] += 0.001  # fake finite time, never sleeps
            return clock_ticks["t"]

        runner = _Runner([[{"title": "A", "url": "https://example.org/a"}]])
        t0 = time.perf_counter()
        cold_bridge = sb.build_native_bridge({}, store=store, watch_id="w1",
                                             watch_version=1, runner=runner, wall_clock=wall)
        cold = cold_bridge.search("quantum networking")
        report["cold"] = {
            "status": cold.status, "backend_calls": runner.search_calls,
            "model_calls": runner.model_calls, "http_calls": runner.http_calls,
            "from_cache": cold.from_cache,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 3),
        }
        runner2 = _Runner([[{"title": "B", "url": "https://example.org/b"}]])
        t1 = time.perf_counter()
        warm_bridge = sb.build_native_bridge({}, store=store, watch_id="w1",
                                             watch_version=1, runner=runner2, wall_clock=wall)
        warm = warm_bridge.search("quantum networking")
        report["warm"] = {
            "status": warm.status, "backend_calls": runner2.search_calls,
            "model_calls": runner2.model_calls, "http_calls": runner2.http_calls,
            "from_cache": warm.from_cache,
            "elapsed_ms": round((time.perf_counter() - t1) * 1000, 3),
        }
    return report


class PerfSuiteTests(unittest.TestCase):
    def test_cold_and_warm_are_deterministic_and_warm_makes_no_calls(self):
        report = run_perf_suite()
        # Surface the structured offline fake metrics for the closeout report.
        print("PERF_FAKE_REPORT " + json.dumps(report, sort_keys=True))
        self.assertEqual(report["cold"]["backend_calls"], 1)
        self.assertFalse(report["cold"]["from_cache"])
        self.assertEqual(report["warm"]["backend_calls"], 0)
        self.assertTrue(report["warm"]["from_cache"])
        self.assertEqual(report["warm"]["status"], sb.STATUS_OK)
        # Structured, clearly-labelled fake metrics (not a network benchmark).
        self.assertIn("NOT a network", report["note"])
        for phase in ("cold", "warm"):
            row = report[phase]
            self.assertIsInstance(row["elapsed_ms"], float)
            self.assertGreaterEqual(row["elapsed_ms"], 0.0)
            self.assertEqual(row["model_calls"], 0)
            self.assertEqual(row["http_calls"], 0)
        self.assertEqual(report["cold"]["model_calls"], 0)
        self.assertEqual(report["warm"]["http_calls"], 0)


if __name__ == "__main__":
    unittest.main()
