"""Acceptance: feedback-responsive interests + typed config proposals.

No network, model, live config or private source. Standalone:
  PYTHONPATH=src python3 -m unittest tests/test_adaptation_feedback_proposals.py
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

from proactive_pilot import config_proposals as cp  # noqa: E402
from proactive_pilot import interest_feedback as inf  # noqa: E402
from proactive_pilot.adaptive import collect_adaptive, prepare_adaptive_plan  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.gates import notice_allowed  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.watchstore import (  # noqa: E402
    apply_action,
    load_watchstore,
)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
PREFS_EMPTY = {"schema": 1, "version": 0, "explicit": [], "inferred": [],
               "snoozes": [], "paused": {"active": False}}


def _store(tmp, now=NOW):
    return StateStore(Path(tmp), SeededClock(now.isoformat()))


def _policy(**over):
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    policy["_preferences"] = json.loads(json.dumps(PREFS_EMPTY))
    policy.update(over)
    return policy


def _add(store, topic="quantum networking"):
    return apply_action(store, {"action": "add", "public_topic": topic,
                                "scope": "public",
                                "provenance": "explicit user request"}, NOW)


def _suppression(match="quantum", conditional=False):
    return {"schema": 1, "version": 1, "explicit": [{
        "id": "e1", "kind": "feed", "match": match, "suppress": True,
        "conditional": conditional, "exception": "unless exceptionally fitting",
        "scope": "test", "provenance": "owner", "created_at": NOW.isoformat(),
        "expires_at": ""}], "inferred": [], "snoozes": [],
        "paused": {"active": False}}


class FeedbackResponsiveInterestTests(unittest.TestCase):
    def test_explicit_correction_blocks_next_acquisition_without_retiring_watch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            watch = _add(store)
            policy = _policy()
            policy["_preferences"] = _suppression("quantum")
            prepared = prepare_adaptive_plan(policy, SeededClock(NOW.isoformat()),
                                             store=store, planner=None, now=NOW)
            self.assertNotEqual(prepared["status"], "prepared")
            kept = load_watchstore(store)["watches"][watch["id"]]
            self.assertEqual(kept["status"], "active")
            self.assertEqual(kept["public_topic"], "quantum networking")

    def test_conditional_exception_is_not_blanket_suppression(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _add(store)
            policy = _policy()
            policy["_preferences"] = _suppression("quantum", conditional=True)
            prepared = prepare_adaptive_plan(policy, SeededClock(NOW.isoformat()),
                                             store=store, planner=None, now=NOW)
            self.assertEqual(prepared["status"], "prepared")
            self.assertTrue(prepared["plan"].get("queries"))

    def test_temporary_snooze_expires_and_same_watch_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _add(store)
            policy = _policy()
            policy["_preferences"] = {
                **PREFS_EMPTY,
                "snoozes": [{"id": "s1", "until": (NOW + timedelta(hours=1)).isoformat(),
                             "kind": "", "match": "quantum", "scope": "watch",
                             "provenance": "owner"}],
            }
            blocked = prepare_adaptive_plan(policy, SeededClock(NOW.isoformat()),
                                            store=store, planner=None, now=NOW)
            self.assertNotEqual(blocked["status"], "prepared")
            later = NOW + timedelta(hours=2)
            resumed = prepare_adaptive_plan(policy, SeededClock(later.isoformat()),
                                            store=store, planner=None, now=later)
            self.assertEqual(resumed["status"], "prepared")

    def test_private_looking_interest_is_never_a_public_query(self):
        from proactive_pilot.adaptive import approved_interest_targets

        policy = _policy(public_interests=[
            "alex@example.com private inbox", "quantum networking",
            "call +65 9123 4567"])
        self.assertEqual(approved_interest_targets(policy), ["quantum networking"])

    def test_paused_collection_reads_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _add(store)
            calls = []
            summary = collect_adaptive(
                _policy(), SeededClock(NOW.isoformat()), store=store,
                fetcher=lambda *a, **k: calls.append(a), now=NOW, paused=True)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(calls, [])

    def test_thanks_is_a_noop_not_disinterest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            watch = _add(store)
            before = load_watchstore(store)["watches"][watch["id"]]
            result = inf.acknowledgement_noop()
            after = load_watchstore(store)["watches"][watch["id"]]
            self.assertEqual(result["status"], "no-op")
            self.assertEqual(before["status"], after["status"])
            self.assertEqual(before["version"], after["version"])

    def test_matching_evidence_is_filtered_but_nonmatching_survives(self):
        from proactive_pilot.collectors import SourceResult

        policy = _policy()
        policy["_preferences"] = _suppression("quantum")
        result = SourceResult("feed:x", "ok", count=2, items=[
            {"title": "Quantum networking breakthrough", "summary": ""},
            {"title": "Local weather", "summary": ""}], checked_at=NOW.isoformat())
        removed = inf.filter_results([result], policy, NOW)
        self.assertEqual(removed, 1)
        self.assertEqual([i["title"] for i in result.items], ["Local weather"])


class ConfigProposalTests(unittest.TestCase):
    def _registry(self, failures=3, reason="429 rate limited"):
        return {"schema": 1, "sources": {
            "feed:x": {"id": "x", "kind": "rss", "interest": "a",
                       "failures": failures, "stale_reason": reason},
        }}

    def test_typed_proposal_is_specific_deduplicated_and_not_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            policy = _policy(feed_cadence_minutes=60)
            first = cp.observe(store, policy, public_registry=self._registry(), now=NOW)
            second = cp.observe(store, policy, public_registry=self._registry(), now=NOW)
            self.assertEqual(len(first), 1)
            # observe returns newly-created changes, not the same pending row.
            self.assertEqual(second, [])
            registry = cp.load_registry(store)
            self.assertEqual(len(registry["proposals"]), 1)
            proposal = first[0]
            self.assertEqual(proposal["key"], "feed_cadence_minutes")
            self.assertEqual(proposal["before"], 60)
            self.assertEqual(proposal["after"], 120)
            self.assertIn("approval", proposal)
            # Configuration itself remains unchanged until foreground approval.
            self.assertEqual(policy["feed_cadence_minutes"], 60)
            self.assertNotIn("scope", proposal["key"])

    def test_proposal_is_expiring_and_dismissible(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            policy = _policy(feed_cadence_minutes=60)
            created = cp.observe(store, policy, public_registry=self._registry(), now=NOW)
            pid = created[0]["id"]
            notice = cp.pending_notice(store, NOW)
            self.assertEqual(notice[0], pid)
            cp.mark_presented(store, pid, NOW)
            self.assertIsNone(cp.pending_notice(store, NOW))
            cp.dismiss(store, pid, NOW)
            self.assertEqual(cp.load_registry(store)["proposals"][pid]["status"], "dismissed")
            # An expired pending proposal is never presented.
            registry = cp.load_registry(store)
            registry["proposals"][pid].update({
                "status": "pending", "presented_at": "",
                "expires_at": (NOW - timedelta(minutes=1)).isoformat()})
            cp.save_registry(store, registry)
            self.assertIsNone(cp.pending_notice(store, NOW))

    def test_no_evidence_no_filler_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            created = cp.observe(store, _policy(), public_registry=self._registry(
                failures=3, reason="http 404"), now=NOW)
            self.assertEqual(created, [])
            self.assertEqual(cp.load_registry(store)["proposals"], {})

    def test_only_typed_allowlisted_keys_can_be_proposed(self):
        registry = cp.empty_registry()
        with self.assertRaises(ValueError):
            cp.add_proposal(registry, key="public_sources", before=1, after=2,
                            reason="expand access", evidence="x", now=NOW)
        with self.assertRaises(ValueError):
            cp.add_proposal(registry, key="feed_cadence_minutes", before=60, after=60,
                            reason="unchanged", evidence="x", now=NOW)

    def test_proposal_uses_same_ordinary_cap_as_alerts(self):
        state = {"delivered": [{"at": NOW.isoformat(), "signature": f"x{i}",
                                "urgent": False} for i in range(5)]}
        allowed, reason = notice_allowed(state, _policy(daily_normal_cap=5),
                                          PREFS_EMPTY, NOW)
        self.assertFalse(allowed)
        self.assertEqual(reason, "daily cap")


if __name__ == "__main__":
    unittest.main()
