"""Broad public discovery fake tests: classification, URL/DNS vetting, reconcile.

No network: the search bridge is faked and DNS resolution uses an injected
resolver table.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import discovery as disc  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.publicfeeds import URLRejected, validate_public_url  # noqa: E402
from proactive_pilot.searchbridge import (  # noqa: E402
    FakeSearchBridge,
    SearchOutcome,
    STATUS_RATE_LIMITED,
)


def _plan(queries=("quantum networking",), scope="public"):
    return {"watch_id": "w_1", "watch_version": 3, "scope": scope,
            "queries": list(queries)}


def _policy(enabled=True):
    return {
        "adaptive_broad_discovery_enabled": enabled,
        "discovery_cadence_minutes": 360,
        "adaptive_registry_limit": 12,
        "native_search_max_discovered_sources": 3,
    }


def _resolver(table):
    def resolve(host):
        if host not in table:
            raise disc.DiscoveryRejected("no fake dns record")
        return table[host]
    return resolve


PUBLIC = _resolver({"news.example": ["93.184.216.34"],
                    "feed.example": ["93.184.216.34"],
                    "api.example": ["93.184.216.34"]})


def _payload(*urls):
    return {"success": True, "data": {"web": [
        {"title": f"Title {i}", "url": u, "description": "d"} for i, u in enumerate(urls)
    ]}}


class BroadDiscoveryTests(unittest.TestCase):
    def test_discovers_and_classifies_public_sources(self):
        bridge = FakeSearchBridge({
            "quantum networking": _payload(
                "https://feed.example/rss.xml",
                "https://api.example/data.json",
                "https://news.example/story",
            )
        })
        sources, summary = disc.discover_public_sources(
            _plan(), _policy(), bridge, resolver=PUBLIC)
        kinds = {s["kind"] for s in sources}
        self.assertEqual(kinds, {"rss", "web_json", "web_page"})
        self.assertEqual(summary["discovered"], 3)
        self.assertEqual(summary["rejected"], 0)

    def test_private_and_mixed_dns_rejected(self):
        for table in (
            {"news.example": ["10.0.0.5"]},
            {"news.example": ["93.184.216.34", "127.0.0.1"]},
            {"news.example": ["::1"]},
            {"news.example": ["169.254.169.254"]},
            {"news.example": []},
        ):
            bridge = FakeSearchBridge({"q": _payload("https://news.example/story")})
            sources, summary = disc.discover_public_sources(
                _plan(("q",)), _policy(), bridge, resolver=_resolver(table))
            self.assertEqual(sources, [], table)
            self.assertGreaterEqual(summary["rejected"], 1)

    def test_credentials_and_secret_query_rejected(self):
        for url in (
            "https://user:pass@news.example/story",
            "https://news.example/story?access_token=leak",
            "http://news.example/story",
            "https://news.example:8443/story",
        ):
            with self.assertRaises(disc.DiscoveryRejected):
                disc.vet_discovered_url(url, resolver=PUBLIC)
            # The bridge itself also drops these before they can become sources.
            bridge = FakeSearchBridge({"q": _payload(url)})
            sources, _summary = disc.discover_public_sources(
                _plan(("q",)), _policy(), bridge, resolver=PUBLIC)
            self.assertEqual(sources, [], url)

    def test_rate_limited_and_empty_register_nothing(self):
        limited = FakeSearchBridge({"q": SearchOutcome(query="q", status=STATUS_RATE_LIMITED)})
        sources, summary = disc.discover_public_sources(_plan(("q",)), _policy(), limited,
                                                        resolver=PUBLIC)
        self.assertEqual(sources, [])
        self.assertEqual(summary["status"], "rate_limited")

        empty = FakeSearchBridge({"q": {"success": True, "data": {"web": []}}})
        sources2, summary2 = disc.discover_public_sources(_plan(("q",)), _policy(), empty,
                                                          resolver=PUBLIC)
        self.assertEqual(sources2, [])
        self.assertEqual(summary2["status"], "empty")

    def test_disabled_policy_does_nothing(self):
        bridge = FakeSearchBridge({"q": _payload("https://news.example/x")})
        sources, summary = disc.discover_public_sources(
            _plan(("q",)), _policy(enabled=False), bridge, resolver=PUBLIC)
        self.assertEqual(sources, [])
        self.assertEqual(summary["status"], "disabled")
        self.assertEqual(bridge.calls, [])

    def test_budget_bounds_queries(self):
        bridge = FakeSearchBridge({q: _payload() for q in ("a", "b", "c")})
        from proactive_pilot.searchbridge import SearchBudget
        sources, summary = disc.discover_public_sources(
            _plan(("a", "b", "c")), _policy(), bridge, budget=SearchBudget(1),
            resolver=PUBLIC)
        self.assertEqual(summary["searched_queries"], 1)
        self.assertEqual(bridge.calls, ["a"])

    def test_ensure_discovered_sources_reconciles_and_retires(self):
        clock = SeededClock("2026-10-03T12:00:00")
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            registry = {"schema": 1, "updated_at": "", "feed_cursor": 0, "sources": {}}
            first = [{"kind": "rss", "url": "https://feed.example/a.rss",
                      "interest": "a", "provenance": "p"}]
            added = disc.ensure_discovered_sources(registry, _plan(), first, _policy(),
                                                   clock.now(), resolver=PUBLIC)
            self.assertEqual(added, 1)
            sid = next(iter(registry["sources"]))
            rec = registry["sources"][sid]
            self.assertTrue(rec["vetted_public"] and rec["discovered"])
            self.assertEqual(rec["watch_id"], "w_1")
            # A new plan revision retires the previous discovered source.
            second = [{"kind": "web_page", "url": "https://news.example/b",
                       "interest": "b", "provenance": "p"}]
            revised = _plan()
            revised["watch_version"] = 4
            disc.ensure_discovered_sources(registry, revised, second, _policy(),
                                           clock.now(), resolver=PUBLIC)
            urls = {r["url"] for r in registry["sources"].values()}
            self.assertEqual(urls, {"https://news.example/b"})

    def test_vet_discovered_url_rejects_ssrf_before_fetch(self):
        with self.assertRaises(disc.DiscoveryRejected):
            disc.vet_discovered_url("https://127.0.0.1/x", resolver=PUBLIC)
        with self.assertRaises(disc.DiscoveryRejected):
            disc.vet_discovered_url("https://news.example/x", resolver=_resolver({}))
        # publicfeeds still rejects an unallowlisted host without the opt-in.
        self.assertRaises(URLRejected, validate_public_url, "https://news.example/x")


if __name__ == "__main__":
    unittest.main()
