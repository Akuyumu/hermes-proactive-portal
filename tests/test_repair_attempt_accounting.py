"""Regression: failed/deferred repair routes stay neutral; real probes charge."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import publicfeeds as pf  # noqa: E402
from proactive_pilot import source_maintenance as sm  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.searchbridge import FakeSearchBridge, SearchBudget  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
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


def _registry():
    return {"schema": 1, "sources": {
        "rss:dead": {"id": "rss:dead", "kind": "rss",
                     "url": "https://www.reddit.com/r/quantum/.rss",
                     "host": "www.reddit.com", "interest": "quantum networking",
                     "scope": "public", "cadence_minutes": 60, "failures": 4,
                     "stale_reason": "http 404",
                     "cache_items": [{"title": "old"}]},
    }}


def _success_bridge(url="https://fresh.example/feed.rss"):
    return FakeSearchBridge({"quantum networking": {"success": True, "data": {"web": [
        {"title": "Quantum networking updates", "url": url,
         "description": "fresh continuing coverage"}]}}})


def _probe(count=2):
    return lambda kind, url, interest: SourceResult(
        "feed:probe", "ok", count=count,
        items=[{"title": f"fresh {i}"} for i in range(count)],
        checked_at=NOW.isoformat())


def _decider(equivalent=True):
    return lambda rec, candidate, result: {
        "equivalent": equivalent, "purpose": "quantum networking updates",
        "authority": "reputable_public", "date_scope_ok": True,
        "reason": "same purpose" if equivalent else "only chatter"}


def _run(registry, *, policy, bridge, probe=None, decider=None, budget=None,
         privacy_verifier=None):
    return sm.attempt_replacements(
        registry, policy=policy, now=NOW, probe=probe or _probe(),
        decider=decider or _decider(), search_bridge=bridge,
        search_budget=SearchBudget(4), overall_budget=budget,
        resolver=lambda host: PUBLIC_IP, can_attempt=lambda: True,
        privacy_verifier=privacy_verifier)


class _ResidualBudget:
    def __init__(self, remaining):
        self._remaining = float(remaining)

    def remaining(self):
        return self._remaining

    def take(self, want):
        return int(want)


class _AllowVerifier:
    def __init__(self):
        self.calls = 0

    def review(self, topic, include, exclude, context, policy, *, timeout=None):
        self.calls += 1
        return {"schema": 1, "decision": "allow", "reason": "", "safe_topic": ""}


class _RejectVerifier:
    def __init__(self):
        self.calls = 0

    def review(self, topic, include, exclude, context, policy, *, timeout=None):
        self.calls += 1
        return {"schema": 1, "decision": "reject", "reason": "private",
                "safe_topic": ""}


class NeutralAccountingTests(unittest.TestCase):
    def test_repeated_failed_discovery_stays_neutral_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            store.save(sm.PUBLIC_REGISTRY, _registry())
            failing = FakeSearchBridge(
                default={"success": False, "error": "provider down"})
            for _ in range(4):
                registry = store.load(sm.PUBLIC_REGISTRY, pf.new_registry())
                events = _run(registry, policy=_policy(), bridge=failing)
                store.save(sm.PUBLIC_REGISTRY, registry)
                rec = registry["sources"]["rss:dead"]
                self.assertEqual(events, [])
                self.assertEqual(rec["maintenance"].get("attempts", 0), 0)
                self.assertFalse(rec["maintenance"].get("next_attempt_at"))
                self.assertEqual(rec["maintenance"]["last_outcome"]["status"],
                                 "failed")
                self.assertEqual(rec["maintenance"]["last_search"]["status"],
                                 "failed")
            self.assertEqual(len(failing.calls), 4)

            reloaded = store.load(sm.PUBLIC_REGISTRY, pf.new_registry())
            rec = reloaded["sources"]["rss:dead"]
            self.assertEqual(rec["maintenance"].get("attempts", 0), 0)
            self.assertNotIn("next_attempt_at", rec["maintenance"])

            recovering = _success_bridge()
            events = _run(reloaded, policy=_policy(), bridge=recovering)
            store.save(sm.PUBLIC_REGISTRY, reloaded)
            self.assertEqual(len(events), 1)
            self.assertEqual(recovering.calls, ["quantum networking"])
            rec = reloaded["sources"]["rss:dead"]
            self.assertEqual(rec["maintenance"]["attempts"], 1)
            self.assertTrue(rec["parked"])
            self.assertEqual(events[0]["old_id"], "rss:dead")

    def test_real_candidate_probe_is_charged_even_when_rejected(self):
        registry = _registry()
        events = _run(registry, policy=_policy(), bridge=_success_bridge(),
                      decider=_decider(equivalent=False))
        self.assertEqual(events, [])
        rec = registry["sources"]["rss:dead"]
        self.assertEqual(rec["maintenance"]["attempts"], 1)
        self.assertTrue(rec["maintenance"].get("next_attempt_at"))
        self.assertEqual(rec["maintenance"]["last_outcome"]["status"],
                         "not_equivalent")

    def test_privacy_budget_deferral_is_neutral_then_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            store.save(sm.PUBLIC_REGISTRY, _registry())
            policy = _policy(adaptive_privacy_review_enabled=True)
            verifier = _AllowVerifier()
            budget = _ResidualBudget(0.0)
            bridge = _success_bridge()
            registry = store.load(sm.PUBLIC_REGISTRY, pf.new_registry())
            events = _run(registry, policy=policy, bridge=bridge,
                          budget=budget, privacy_verifier=verifier)
            store.save(sm.PUBLIC_REGISTRY, registry)
            self.assertEqual(events, [])
            self.assertEqual(bridge.calls, [])
            self.assertEqual(verifier.calls, 0)
            rec = registry["sources"]["rss:dead"]
            self.assertEqual(rec["maintenance"].get("attempts", 0), 0)
            self.assertFalse(rec["maintenance"].get("next_attempt_at"))
            self.assertEqual(rec["maintenance"]["last_outcome"]["status"],
                             "budget_deferred")
            self.assertEqual(rec["maintenance"]["last_search"]["status"],
                             "budget_deferred")
            self.assertIn("residual time",
                          rec["maintenance"]["last_search"]["reason"])

            reloaded = store.load(sm.PUBLIC_REGISTRY, pf.new_registry())
            budget._remaining = 5.0
            recovering = _success_bridge()
            events = _run(reloaded, policy=policy, bridge=recovering,
                          budget=budget, privacy_verifier=verifier)
            store.save(sm.PUBLIC_REGISTRY, reloaded)
            self.assertEqual(len(events), 1)
            self.assertEqual(verifier.calls, 1)
            self.assertEqual(recovering.calls, ["quantum networking"])
            self.assertEqual(
                reloaded["sources"]["rss:dead"]["maintenance"]["attempts"], 1)

    def test_unavailable_privacy_and_revoked_routes_never_acquire(self):
        bridge = _success_bridge()
        registry = _registry()
        events = _run(registry,
                      policy=_policy(adaptive_broad_discovery_enabled=False),
                      bridge=bridge)
        self.assertEqual(events, [])
        self.assertEqual(bridge.calls, [])
        rec = registry["sources"]["rss:dead"]
        self.assertEqual(rec["maintenance"]["last_outcome"]["status"],
                         "no_authorised_route")
        self.assertEqual(rec["maintenance"].get("attempts", 0), 0)
        self.assertFalse(rec["maintenance"].get("next_attempt_at"))

        reject = _RejectVerifier()
        registry = _registry()
        events = _run(registry,
                      policy=_policy(adaptive_privacy_review_enabled=True),
                      bridge=_success_bridge(), privacy_verifier=reject)
        self.assertEqual(events, [])
        self.assertEqual(reject.calls, 1)
        rec = registry["sources"]["rss:dead"]
        self.assertEqual(rec["maintenance"]["last_outcome"]["status"],
                         "privacy_rejected")
        self.assertEqual(rec["maintenance"].get("attempts", 0), 0)
        self.assertFalse(rec["maintenance"].get("next_attempt_at"))

        prefs = {"schema": 1, "version": 0,
                 "explicit": [{"kind": "feed", "suppress": True,
                               "match": "quantum"}],
                 "snoozes": [], "paused": {"active": False}}
        revoked_bridge = _success_bridge()
        registry = _registry()
        events = _run(registry, policy=_policy(_preferences=prefs),
                      bridge=revoked_bridge)
        self.assertEqual(events, [])
        self.assertEqual(revoked_bridge.calls, [])
        rec = registry["sources"]["rss:dead"]
        self.assertEqual(rec["maintenance"]["last_outcome"]["status"],
                         "preference_blocked")
        self.assertEqual(rec["maintenance"].get("attempts", 0), 0)
        self.assertFalse(rec["maintenance"].get("next_attempt_at"))


if __name__ == "__main__":
    unittest.main()
