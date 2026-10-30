"""Final integration regressions: no-op writes, privacy, typing, feedback."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import config_proposals as cp  # noqa: E402
from proactive_pilot import source_maintenance as sm  # noqa: E402
from proactive_pilot.interestproposal import PrivacyReviewError  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.searchbridge import SearchBudget  # noqa: E402

NOW = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)


def _policy(**over):
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    policy.update({"adaptive_mode": "active",
                   "adaptive_broad_discovery_enabled": True})
    policy.update(over)
    return policy


def _rec(**over):
    rec = {"id": "old", "kind": "rss", "url": "https://example.org/old",
           "host": "example.org", "interest": "public astronomy releases",
           "failures": 4, "stale_reason": "http 404"}
    rec.update(over)
    return rec


def _candidate(kind="rss", url="https://example.org/new", host="example.org"):
    return {"kind": kind, "url": url, "host": host, "vetted_public": True}


def _decision(**over):
    data = {"equivalent": True, "purpose": "astronomy releases",
            "authority": "reputable_public", "date_scope_ok": True,
            "reason": "equivalent authoritative feed"}
    data.update(over)
    return data


class _CountingStore:
    def __init__(self, data=None):
        self.data = data or {}
        self.saves = 0

    def load(self, name, default):
        return json.loads(json.dumps(self.data.get(name, default)))

    def save(self, name, payload):
        self.saves += 1
        self.data[name] = json.loads(json.dumps(payload))


class ProposalNoOpTests(unittest.TestCase):
    def test_empty_observe_pending_show_write_nothing(self):
        store = _CountingStore()
        policy = _policy(feed_cadence_minutes=60)
        self.assertEqual(cp.observe(store, policy, public_registry={"sources": {}}, now=NOW), [])
        self.assertIsNone(cp.pending_notice(store, NOW, policy=policy,
                                           public_registry={"sources": {}}))
        self.assertIsNone(cp.show(store, "missing", NOW))
        self.assertEqual(store.saves, 0)

    def test_handoff_has_actual_settings_patch(self):
        store = _CountingStore()
        policy = _policy(feed_cadence_minutes=60)
        created = cp.observe(store, policy, public_registry={"sources": {
            "x": {"id": "x", "kind": "rss", "failures": 4,
                  "stale_reason": "429 rate limited"}}}, now=NOW)
        result = cp.approval_handoff(store, created[0]["id"], policy, now=NOW,
                                     public_registry={"sources": {
                                         "x": {"id": "x", "kind": "rss",
                                               "failures": 4,
                                               "stale_reason": "429 rate limited"}}})
        self.assertEqual(result["settings_path"],
                         "advanced.legacy_policy.feed_cadence_minutes")
        self.assertEqual(result["settings_change"],
                         {"advanced": {"legacy_policy":
                                       {"feed_cadence_minutes": 120}}})
        self.assertFalse(result["applied"])


class SourceLifecycleIntegrationTests(unittest.TestCase):
    def _run(self, rec, *, policy=None, probe=None, decider=None,
             bridge=None, privacy_verifier=None):
        with patch.object(sm, "search_alternatives",
                          return_value=([_candidate()], {"status": "ok"})):
            return sm.attempt_replacements(
                {"sources": {"old": rec}}, policy=policy or _policy(), now=NOW,
                probe=probe or (lambda *a: SimpleNamespace(
                    status="ok", cached=False, count=1, error="",
                    checked_at=NOW.isoformat(),
                    items=[{"title": "fresh", "date": NOW.isoformat()}])),
                decider=decider or (lambda *a: _decision()), search_bridge=bridge,
                search_budget=SearchBudget(2), can_attempt=lambda: True,
                privacy_verifier=privacy_verifier)

    def test_naive_next_attempt_does_not_crash_cycle(self):
        rec = _rec(maintenance={"attempts": 1,
                                "next_attempt_at": "2030-01-02T13:00:00"})
        events = self._run(rec)
        self.assertEqual(events, [])
        self.assertEqual(rec["maintenance"]["attempts"], 1)

    def test_scoped_snooze_blocks_repair_but_expiry_permits(self):
        prefs = {"schema": 1, "version": 0, "explicit": [], "inferred": [],
                 "snoozes": [{"id": "s", "until": (NOW + timedelta(hours=1)).isoformat(),
                              "kind": "feed", "match": "astronomy", "scope": "w",
                              "provenance": "owner"}],
                 "paused": {"active": False}}
        blocked = self._run(_rec(), policy=_policy(_preferences=prefs))
        self.assertEqual(blocked, [])
        prefs["snoozes"][0]["until"] = (NOW - timedelta(minutes=1)).isoformat()
        allowed = self._run(_rec(), policy=_policy(_preferences=prefs))
        self.assertEqual(len(allowed), 1)

    def test_typed_official_kind_is_preserved_on_adoption(self):
        rec = _rec(kind="smrt_notice", url="https://www.smrt.com.sg/old",
                   host="www.smrt.com.sg",
                   provenance="public vetted seed (official SMRT planned notice)")
        expired_free = SimpleNamespace(
            status="ok", cached=False, count=1, error="", checked_at=NOW.isoformat(),
            items=[{"title": "Planned notice",
                    "valid_until": (NOW + timedelta(hours=2)).isoformat()}])
        with patch.object(sm, "search_alternatives", return_value=(
                [{"kind": "web_page", "url": "https://www.smrt.com.sg/new",
                  "host": "www.smrt.com.sg", "vetted_public": True}], {"status": "ok"})):
            events = sm.attempt_replacements(
                {"sources": {"old": rec}}, policy=_policy(), now=NOW,
                probe=lambda *a: expired_free,
                decider=lambda *a: _decision(authority="official"),
                search_bridge=object(), search_budget=SearchBudget(1),
                can_attempt=lambda: True)
        self.assertEqual(len(events), 1)
        new = events[0]["new_id"]
        # Look up by scanning is avoided: event carries the adopted kind below.
        self.assertEqual(new.split(":")[0], "smrt_notice")

    def test_semantic_privacy_reject_prevents_search_and_probe(self):
        calls = []
        bridge = SimpleNamespace(search=lambda *a, **k: calls.append("search"))

        class Reject:
            def review(self, topic, include, exclude, context, policy, *, timeout=None):
                return {"schema": 1, "decision": "reject", "reason": "private",
                        "safe_topic": ""}

        policy = _policy(adaptive_privacy_review_enabled=True)
        rec = _rec()
        # Use the real search path (not patched) so zero-search is meaningful.
        events = sm.attempt_replacements(
            {"sources": {"old": rec}}, policy=policy, now=NOW,
            probe=lambda *a: calls.append("probe"),
            decider=lambda *a: _decision(), search_bridge=bridge,
            search_budget=SearchBudget(1), can_attempt=lambda: True,
            privacy_verifier=Reject())
        self.assertEqual(events, [])
        self.assertEqual(calls, [])
        self.assertEqual(rec["maintenance"]["last_outcome"]["status"],
                         "privacy_rejected")


if __name__ == "__main__":
    unittest.main()
