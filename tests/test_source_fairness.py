"""Source replacement / coverage fairness controls (fake only, no network).

Proves the reviewed blocker: the top-ranked discovered sites being persistently
unreachable must NOT permanently starve other viable sources. A bounded
alternative pool + per-URL failure backoff rotate a quarantined slot to the next
viable candidate, with NO cap increase. Also proves the adaptive summary reports
PIPELINE completion separately from complete SOURCE coverage.

No live HTTP, no subprocess, no model, no /tmp, nothing sent. Standalone:
  PYTHONPATH=src python3 -m unittest tests/test_source_fairness.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import discovery as disc  # noqa: E402
from proactive_pilot.adaptive import collect_adaptive  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import Budget  # noqa: E402
from proactive_pilot.planner import FakePlanner  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.publicfeeds import FetchResponse  # noqa: E402
from proactive_pilot.searchbridge import FakeSearchBridge  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.watchstore import apply_action  # noqa: E402

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def _public(host):
    return ["93.184.216.34"]


def _plan(version=1):
    return {"watch_id": "w_1", "watch_version": version, "scope": "public",
            "queries": ["q"]}


def _policy(**over):
    policy = {"adaptive_broad_discovery_enabled": True,
              "discovery_cadence_minutes": 360,
              "adaptive_registry_limit": 12,
              "native_search_max_discovered_sources": 2,
              "native_search_discovered_failure_threshold": 2,
              "native_search_discovered_backoff_minutes": 360}
    policy.update(over)
    return policy


def _pool(*letters):
    return [{"kind": "web_page", "url": f"https://{c}.example/story",
             "interest": c, "provenance": "p"} for c in letters]


def _urls(registry):
    return {str(rec.get("url")) for rec in registry["sources"].values()
            if rec.get("discovered")}


def _fail(registry, now):
    for rec in registry["sources"].values():
        if rec.get("discovered"):
            rec["failures"] = 2
            rec["last_fetch_at"] = now.isoformat()
    disc.note_discovered_failure_state(registry, now)


class SourceReplacementFairness(unittest.TestCase):
    def test_unreachable_top_sources_replaced_by_bounded_alternatives(self):
        registry = {"schema": 1, "sources": {}}
        first = disc.ensure_discovered_sources(registry, _plan(), _pool("a", "b", "c", "d"),
                                               _policy(), NOW, resolver=_public)
        self.assertEqual(first, 2)
        self.assertEqual(_urls(registry), {"https://a.example/story",
                                           "https://b.example/story"})
        # Both top sources fail past the threshold -> quarantined.
        _fail(registry, NOW)
        # The same pool must now surface the next viable alternatives, NOT the
        # same unreachable top-two forever.
        disc.ensure_discovered_sources(registry, _plan(), _pool("a", "b", "c", "d"),
                                       _policy(), NOW, resolver=_public)
        self.assertEqual(_urls(registry), {"https://c.example/story",
                                           "https://d.example/story"})
        # Cap is unchanged (no increase to fit alternatives).
        self.assertLessEqual(len(_urls(registry)), 2)

    def test_quarantine_expires_after_backoff(self):
        registry = {"schema": 1, "sources": {}}
        disc.ensure_discovered_sources(registry, _plan(), _pool("a", "b", "c", "d"),
                                       _policy(), NOW, resolver=_public)
        _fail(registry, NOW)
        later = NOW + timedelta(minutes=361)
        disc.ensure_discovered_sources(registry, _plan(), _pool("a", "b", "c", "d"),
                                       _policy(), later, resolver=_public)
        # After the backoff the top-ranked source is retried, not blacklisted.
        self.assertEqual(_urls(registry), {"https://a.example/story",
                                           "https://b.example/story"})

    def test_healthy_success_clears_backoff(self):
        registry = {"schema": 1, "sources": {}}
        disc.ensure_discovered_sources(registry, _plan(), _pool("a", "b"),
                                       _policy(), NOW, resolver=_public)
        _fail(registry, NOW)
        self.assertTrue(registry.get("discovered_failures"))
        for rec in registry["sources"].values():
            rec["failures"] = 0
        disc.note_discovered_failure_state(registry, NOW)
        self.assertFalse(registry.get("discovered_failures"))

    def test_discovery_returns_bounded_pool_above_registration_cap(self):
        bridge = FakeSearchBridge({"q": {"success": True, "data": {"web": [
            {"title": f"t{i}", "url": f"https://h{i}.example/x"} for i in range(5)]}}})
        sources, summary = disc.discover_public_sources(
            _plan(), _policy(native_search_max_discovered_sources=2), bridge,
            resolver=_public)
        self.assertEqual(summary["registration_cap"], 2)
        self.assertEqual(len(sources), 5)
        self.assertLessEqual(len(sources), summary["pool_cap"])


class PipelineVsCoverageStatus(unittest.TestCase):
    def _run(self, fetch_status):
        clock = SeededClock(NOW.isoformat())
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), clock)
            apply_action(store, {"action": "add", "public_topic": "quantum",
                                 "scope": "public", "provenance": "explicit"},
                         clock.now())
            policy = dict(DEFAULT_POLICY)
            policy.update(adaptive_mode="active", adaptive_broad_discovery_enabled=True,
                          native_search_max_discovered_sources=1,
                          adaptive_fetch_max_sources_per_run=4,
                          feed_max_sources_per_run=4)
            planner = FakePlanner({"schema": 1, "queries": ["quantum"], "scope": "public",
                                   "semantic": True, "provenance": "model"})
            bridge = FakeSearchBridge({"quantum": {"success": True, "data": {"web": [
                {"title": "u", "url": "https://news.example/story"}]}}})
            fetcher = _fetcher(fetch_status)
            return collect_adaptive(policy, clock, store=store, planner=planner,
                                    fetcher=fetcher, budget=Budget(30),
                                    search_bridge=bridge, resolver=_public)

    def test_unreachable_source_is_pipeline_completed_not_complete_coverage(self):
        summary = self._run(403)
        self.assertEqual(summary["pipeline_status"], "completed")
        self.assertIn(summary["coverage_status"], {"partial", "unavailable"})
        self.assertNotEqual(summary["coverage_status"], "complete")

    def test_readable_source_is_complete_coverage(self):
        summary = self._run(200)
        self.assertEqual(summary["pipeline_status"], "completed")
        self.assertEqual(summary["coverage_status"], "complete")


def _fetcher(discovered_status):
    body = b"<html><head><title>Story</title></head><body>quantum update</body></html>"

    def fetch(url, **kwargs):
        if "news.example" in url:
            if discovered_status == 200:
                return FetchResponse(200, {"Content-Type": "text/html"}, body, url)
            return FetchResponse(int(discovered_status), {}, b"denied", url)
        if "reddit.com" in url:
            return FetchResponse(200, {}, b"<rss></rss>", url)
        if "api.github.com" in url:
            return FetchResponse(200, {}, b'{"items": []}', url)
        raise AssertionError(f"unexpected fetch: {url}")

    return fetch


if __name__ == "__main__":
    unittest.main()
