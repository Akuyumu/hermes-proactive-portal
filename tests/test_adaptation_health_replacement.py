"""Acceptance: truthful public health + verified equivalent replacement.

Corrected after parent findings: a same-topic search hit is NOT an equivalent.
Replacement now requires broad vetted discovery, a strict structured equivalence
decision, a fresh usable fetch and preserved evidence. No network is used.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import publicfeeds as pf  # noqa: E402
from proactive_pilot import source_maintenance as sm  # noqa: E402
from proactive_pilot import source_truth  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.searchbridge import FakeSearchBridge, SearchBudget  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
PUBLIC_IP = ["93.184.216.34"]


def _store(tmp):
    return StateStore(Path(tmp), SeededClock(NOW.isoformat()))


def _policy(**over):
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    policy.update({
        "public_source_maintenance_enabled": True,
        "adaptive_broad_discovery_enabled": True,
        "adaptive_registry_limit": 12,
        "native_search_max_discovered_sources": 2,
    })
    policy.update(over)
    return policy


def _registry(kind="rss", url="https://www.reddit.com/r/quantum/.rss", seed=False):
    return {"schema": 1, "sources": {
        "rss:dead": {"id": "rss:dead", "kind": kind, "url": url,
                     "host": pf.canonical_host(url.split("/")[2]),
                     "interest": "quantum networking", "scope": "public",
                     "cadence_minutes": 60, "failures": 4,
                     "stale_reason": "http 404", "seed": seed,
                     "cache_items": [{"title": "old"}],
                     "cache_at": (NOW - timedelta(days=3)).isoformat()},
    }}


def _bridge(url="https://fresh.example/feed.rss"):
    return FakeSearchBridge({"quantum networking": {"success": True, "data": {"web": [
        {"title": "Quantum networking official updates", "url": url,
         "description": "fresh continuing coverage"}]}}})


def _decider(equivalent=True, authority="reputable_public"):
    return lambda rec, candidate, result: {
        "equivalent": equivalent, "purpose": "quantum networking updates",
        "authority": authority, "date_scope_ok": True,
        "reason": "same purpose" if equivalent else "only chatter"}


def _probe(count=2):
    return lambda kind, url, interest: SourceResult(
        "feed:probe", "ok", count=count, items=[{"title": f"fresh {i}"} for i in range(count)],
        checked_at=NOW.isoformat())


class HealthTruthTests(unittest.TestCase):
    def test_partial_coverage_is_not_hidden_by_one_success(self):
        registry = {"schema": 1, "sources": {
            "feed:ok": {"id": "ok", "kind": "rss", "interest": "a",
                        "cadence_minutes": 60, "last_fetch_at": NOW.isoformat(),
                        "cache_at": NOW.isoformat()},
            "feed:dead": {"id": "dead", "kind": "rss", "interest": "b",
                          "cadence_minutes": 60, "last_fetch_at": NOW.isoformat(),
                          "failures": 4, "stale_reason": "http 404"},
        }}
        health = {"sources": {
            "feed:ok": {"last_observation_kind": "success",
                        "last_success_at": NOW.isoformat()},
            "feed:dead": {"last_observation_kind": "failure",
                          "last_success_at": (NOW - timedelta(days=2)).isoformat(),
                          "failure_streak": 4, "failure_class": "other"},
        }}
        truth = source_truth.public_truth(
            {"sources": {"public_feeds": True}, "public_sources": ["a"]},
            {"source_health": {"sources": health}}, registry=registry, now=NOW)
        self.assertEqual(truth["coverage"], "partial")

    def test_unsupported_adaptive_route_is_explicit(self):
        coverage = {"watches": {"w1": {"last_attempt_at": NOW.isoformat(), "sources": [
            {"kind": "adaptive", "status": "unsupported",
             "reason": "scope has no supported public route"}]}}}
        truth = source_truth.public_truth(
            {"sources": {"public_feeds": True}, "public_sources": ["a"]},
            {}, registry={"schema": 1, "sources": {}}, adaptive=coverage, now=NOW)
        self.assertEqual(truth["coverage"], "unsupported")


class ReplacementUnitTests(unittest.TestCase):
    def test_verified_equivalent_is_adopted_with_preserved_evidence(self):
        registry = _registry()
        events = sm.attempt_replacements(
            registry, policy=_policy(), now=NOW, probe=_probe(),
            decider=_decider(), search_bridge=_bridge(), search_budget=SearchBudget(2),
            resolver=lambda host: PUBLIC_IP, can_attempt=lambda: True)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["old_id"], "rss:dead")
        self.assertTrue(event["id"].startswith("sr-"))
        self.assertTrue(registry["sources"]["rss:dead"]["parked"])
        new = registry["sources"][event["new_id"]]
        self.assertEqual(new["replacement_for"], "rss:dead")
        self.assertEqual(new["interest"], "quantum networking")
        self.assertEqual(len(new["cache_items"]), 2)
        self.assertEqual(new["cache_at"], NOW.isoformat())
        self.assertIn("evidence", json.dumps(event))

    def test_same_topic_chatter_is_rejected_by_strict_decision(self):
        registry = _registry()
        events = sm.attempt_replacements(
            registry, policy=_policy(), now=NOW, probe=_probe(),
            decider=_decider(equivalent=False), search_bridge=_bridge(),
            search_budget=SearchBudget(2), resolver=lambda host: PUBLIC_IP,
            can_attempt=lambda: True)
        self.assertEqual(events, [])
        self.assertFalse(registry["sources"]["rss:dead"].get("parked"))

    def test_official_source_cannot_be_replaced_by_unofficial_chatter(self):
        registry = _registry(kind="smrt_notice",
                             url="https://www.smrt.com.sg/notice")
        bridge = FakeSearchBridge({"quantum networking": {"success": True, "data": {"web": [
            {"title": "forum chatter", "url": "https://fresh.example/feed.rss",
             "description": "unofficial"}]}}})
        events = sm.attempt_replacements(
            registry, policy=_policy(), now=NOW, probe=_probe(),
            decider=_decider(authority="official"), search_bridge=bridge,
            search_budget=SearchBudget(2), resolver=lambda host: PUBLIC_IP,
            can_attempt=lambda: True)
        self.assertEqual(events, [])  # unofficial host rejected before decision
        self.assertFalse(registry["sources"]["rss:dead"].get("parked"))

    def test_fresh_generic_status_cannot_revive_expired_official_notice(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        registry = _registry(kind="smrt_notice",
                             url="https://www.smrt.com.sg/old")
        candidate = {"kind": "web_page", "url": "https://www.smrt.com.sg/new",
                     "host": "www.smrt.com.sg", "vetted_public": True}
        expired = SimpleNamespace(
            status="ok", cached=False, count=1, error="", checked_at=NOW.isoformat(),
            items=[{"title": "Old planned notice",
                    "valid_until": (NOW - timedelta(hours=2)).isoformat()}])
        with patch.object(sm, "search_alternatives",
                          return_value=([candidate], {"status": "ok"})):
            events = sm.attempt_replacements(
                registry, policy=_policy(), now=NOW, probe=lambda *a: expired,
                decider=_decider(authority="official"),
                search_bridge=_bridge(), search_budget=SearchBudget(2),
                resolver=lambda host: PUBLIC_IP, can_attempt=lambda: True)
        self.assertEqual(events, [])
        self.assertFalse(registry["sources"]["rss:dead"].get("parked"))

    def test_bogus_authority_enum_is_rejected(self):
        registry = _registry()
        events = sm.attempt_replacements(
            registry, policy=_policy(), now=NOW, probe=_probe(),
            decider=_decider(authority="whatever"), search_bridge=_bridge(),
            search_budget=SearchBudget(2), resolver=lambda host: PUBLIC_IP,
            can_attempt=lambda: True)
        self.assertEqual(events, [])

    def test_missing_strict_lane_or_empty_or_cached_evidence_is_not_adoption(self):
        for decider, probe in ((None, _probe()), (_decider(), _probe(count=0)),
                               (_decider(), lambda k, u, i: SourceResult(
                                   "p", "ok", count=2, items=[{"title": "x"}],
                                   checked_at=NOW.isoformat(), cached=True))):
            registry = _registry()
            events = sm.attempt_replacements(
                registry, policy=_policy(), now=NOW, probe=probe, decider=decider,
                search_bridge=_bridge(), search_budget=SearchBudget(2),
                resolver=lambda host: PUBLIC_IP, can_attempt=lambda: True)
            self.assertEqual(events, [])
            self.assertFalse(registry["sources"]["rss:dead"].get("parked"))

    def test_no_authorised_route_is_honest_not_fixed_github_reddit(self):
        registry = _registry()
        events = sm.attempt_replacements(
            registry, policy=_policy(adaptive_broad_discovery_enabled=False),
            now=NOW, probe=_probe(), decider=_decider(), search_bridge=_bridge(),
            search_budget=SearchBudget(2), resolver=lambda host: PUBLIC_IP,
            can_attempt=lambda: True)
        self.assertEqual(events, [])
        outcome = registry["sources"]["rss:dead"]["maintenance"]["last_outcome"]
        self.assertEqual(outcome["status"], "no_authorised_route")

    def test_transient_and_rate_limited_failures_do_not_churn(self):
        for reason in ("timeout", "429 rate limited", "collector budget exhausted"):
            registry = _registry()
            registry["sources"]["rss:dead"]["stale_reason"] = reason
            if reason == "timeout":
                # This contract is for an isolated transient. Chronic attempted
                # timeouts at the existing threshold are tested separately in
                # test_chronic_source_repair; rate limits and deferrals below
                # keep the high-failure fixture and must still never churn.
                registry["sources"]["rss:dead"]["failures"] = 1
            called = []
            events = sm.attempt_replacements(
                registry, policy=_policy(), now=NOW,
                probe=lambda k, u, i: called.append(u), decider=_decider(),
                search_bridge=_bridge(), search_budget=SearchBudget(2),
                resolver=lambda host: PUBLIC_IP, can_attempt=lambda: True)
            self.assertEqual(events, [], reason)
            self.assertEqual(called, [], reason)

    def test_private_or_unsafe_query_is_rejected_before_search(self):
        registry = _registry()
        registry["sources"]["rss:dead"]["interest"] = "alex@example.com private inbox"
        called = []
        events = sm.attempt_replacements(
            registry, policy=_policy(), now=NOW,
            probe=lambda k, u, i: called.append(u), decider=_decider(),
            search_bridge=_bridge(), search_budget=SearchBudget(2),
            resolver=lambda host: PUBLIC_IP, can_attempt=lambda: True)
        self.assertEqual(events, [])
        self.assertEqual(called, [])
        self.assertEqual(
            registry["sources"]["rss:dead"]["maintenance"]["last_outcome"]["status"],
            "privacy_rejected")

    def test_residual_budget_deferral_makes_no_search_or_probe(self):
        registry = _registry()
        called = []
        events = sm.attempt_replacements(
            registry, policy=_policy(), now=NOW,
            probe=lambda k, u, i: called.append(u), decider=_decider(),
            search_bridge=_bridge(), search_budget=SearchBudget(2),
            resolver=lambda host: PUBLIC_IP, can_attempt=lambda: False)
        self.assertEqual(events, [])
        self.assertEqual(called, [])
        self.assertEqual(
            registry["sources"]["rss:dead"]["maintenance"]["last_outcome"]["status"],
            "budget_deferred")

    def test_consent_gate_keeps_neutral_install_inert(self):
        registry = _registry()
        events = sm.attempt_replacements(
            registry, policy=_policy(public_source_maintenance_enabled=False,
                                     adaptive_mode="disabled"),
            now=NOW, probe=_probe(), decider=_decider(), search_bridge=_bridge(),
            search_budget=SearchBudget(2), resolver=lambda host: PUBLIC_IP,
            can_attempt=lambda: True)
        self.assertEqual(events, [])

    def test_dispute_is_idempotent_and_blocks_same_replacement(self):
        registry = _registry()
        events = sm.attempt_replacements(
            registry, policy=_policy(), now=NOW, probe=_probe(), decider=_decider(),
            search_bridge=_bridge(), search_budget=SearchBudget(2),
            resolver=lambda host: PUBLIC_IP, can_attempt=lambda: True)
        event_id = events[0]["id"]
        new_id = events[0]["new_id"]
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            store.save(sm.PUBLIC_REGISTRY, registry)
            first = sm.dispute_replacement(store, event_id, NOW)
            second = sm.dispute_replacement(store, event_id, NOW)
            restored = store.load(sm.PUBLIC_REGISTRY, pf.new_registry())
        self.assertEqual(first["restored"], second["restored"])
        self.assertFalse(restored["sources"]["rss:dead"]["parked"])
        self.assertTrue(restored["sources"][new_id]["parked"])
        # Original failure history survives the restore (not reset to zero).
        self.assertEqual(restored["sources"]["rss:dead"]["failures"], 4)
        self.assertIn(new_id, restored["rejected_replacements"])

    def test_rejected_replacement_does_not_return_after_later_failures(self):
        registry = _registry()
        events = sm.attempt_replacements(
            registry, policy=_policy(), now=NOW, probe=_probe(), decider=_decider(),
            search_bridge=_bridge(), search_budget=SearchBudget(2),
            resolver=lambda host: PUBLIC_IP, can_attempt=lambda: True)
        new_url = events[0]["new_url"]
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            store.save(sm.PUBLIC_REGISTRY, registry)
            sm.dispute_replacement(store, events[0]["id"], NOW)
            restored = store.load(sm.PUBLIC_REGISTRY, pf.new_registry())
        self.assertIn(new_url, restored["rejected_replacements"])
        restored["sources"]["rss:dead"]["failures"] = 9
        restored["sources"]["rss:dead"]["stale_reason"] = "http 404"
        restored["sources"]["rss:dead"]["maintenance"]["next_attempt_at"] = ""
        events2 = sm.attempt_replacements(
            restored, policy=_policy(), now=NOW, probe=_probe(), decider=_decider(),
            search_bridge=_bridge(), search_budget=SearchBudget(2),
            resolver=lambda host: PUBLIC_IP, can_attempt=lambda: True)
        self.assertEqual(events2, [])


class ReplacementNoticeTests(unittest.TestCase):
    def test_stable_id_one_notice_and_restart_no_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            registry = {"schema": 1, "sources": {}, "replacement_lineage": [{
                "id": "sr-stable-1", "old_id": "rss:dead", "new_id": "rss:new",
                "interest": "quantum networking", "reason": "http 404",
                "at": NOW.isoformat(), "notice_state": ""}]}
            store.save(sm.PUBLIC_REGISTRY, registry)
            first = sm.pending_replacement_notice(store, NOW)
            self.assertEqual(first[0], "sr-stable-1")
            sm.mark_replacement_notice(store, first[0], "emitting", NOW)
            self.assertIsNone(sm.pending_replacement_notice(store, NOW))
            saved = store.load(sm.PUBLIC_REGISTRY, pf.new_registry())
            self.assertEqual(saved["replacement_lineage"][0]["notice_state"], "uncertain")

    def test_adaptive_replacement_is_skipped_for_inactive_watch(self):
        registry = _registry()
        registry["sources"]["rss:dead"]["watch_id"] = "w_off"
        called = []
        events = sm.attempt_replacements(
            registry, policy=_policy(), now=NOW,
            probe=lambda k, u, i: called.append(u), decider=_decider(),
            search_bridge=_bridge(), search_budget=SearchBudget(2),
            resolver=lambda host: PUBLIC_IP, can_attempt=lambda: True,
            watch_active=lambda wid: wid == "w_on")
        self.assertEqual(events, [])
        self.assertEqual(called, [])


class ProductionReplacementSeamTests(unittest.TestCase):
    def test_ordinary_collect_uses_discovery_decision_and_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            registry = _registry()
            registry["sources"]["rss:dead"]["failures"] = 2
            store.save("public_registry.json", registry)
            url = "https://fresh.example/feed.rss"
            rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
                <item><title>Fresh quantum networking update</title>
                <link>https://fresh.example/a</link><description>verified</description>
                <guid>a1</guid></item></channel></rss>"""
            fetched = []

            def fake_fetcher(target, **kwargs):
                fetched.append(target)
                if target == url:
                    return pf.FetchResponse(200, {"content-type": "application/rss+xml"},
                                            rss, target)
                return pf.FetchResponse(404, {}, b"gone", target)

            results = pf.collect_public_feeds(
                _policy(public_sources=["quantum networking"]),
                SeededClock(NOW.isoformat()), store=store, fetcher=fake_fetcher,
                now=NOW, search_bridge=_bridge(url), search_budget=SearchBudget(2),
                maintenance_decider=_decider(), resolver=lambda host: PUBLIC_IP)
            saved = store.load("public_registry.json", pf.new_registry())
            self.assertTrue(results)
            self.assertTrue(saved["sources"]["rss:dead"]["parked"])
            lineage = saved.get("replacement_lineage") or []
            self.assertEqual(len(lineage), 1)
            new = saved["sources"][lineage[0]["new_id"]]
            self.assertEqual(new["replacement_for"], "rss:dead")
            self.assertTrue(new["cache_items"])
            self.assertEqual(new["cache_at"], NOW.isoformat())
            self.assertTrue(any(target == url for target in fetched))


if __name__ == "__main__":
    unittest.main()
