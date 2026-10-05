"""Regression: owner-revoked inferred topics are durably suppressed.

Final narrow acceptance defect: an inferred goal revoked by the owner could be
resurrected by proposing the SAME topic again under a NEW timestamp-based goal
id, because active-topic suppression only considered currently-active goals.

These tests prove the durable tombstone contract:
  * revoke records a normalized (whitespace-collapsed, casefolded) tombstone in
    a registry field SEPARATE from ``goals``;
  * the same topic is rejected across a fresh ``StateStore`` restart, a changed
    context, case/whitespace variation, semantic de-identification and the
    direct ``add_inferred_goal`` path, with ZERO acquisition;
  * clearing the goals dict / TTL sweep never loses the suppression;
  * EXPIRED and SUPERSEDED goals are NOT permanent bans (the same topic may be
    re-proposed);
  * an unrelated safe topic is still allowed.

Offline only: proposer/planner/fetcher/verifier are injected fakes.

  PYTHONPATH=src python3 -m unittest discover -s tests
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import interestproposal as ip  # noqa: E402
from proactive_pilot.adaptive import collect_adaptive  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.planner import FakePlanner  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.shadow import FixtureFetcher  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=SG)
RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Mycorrhizal networks advance</title>
    <link>https://example.com/x1</link>
    <description>fungal research</description>
    <pubDate>Sat, 03 Oct 2026 11:00:00 +0800</pubDate>
    <guid>x1</guid></item>
</channel></rss>"""

CONTEXT = ["Plan mycorrhizal networks garden project", "Read about slime mould computing"]
CHANGED_CONTEXT = ["Trip planning for slime mould computing conference"]


def _policy(**over):
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    policy["_preferences"] = {"explicit": [], "inferred": [], "snoozes": [],
                              "paused": {"active": False}}
    policy["adaptive_inferred_goals_enabled"] = True
    policy["adaptive_mode"] = "active"
    policy["inferred_goals_cooldown_minutes"] = 0
    policy["inferred_goals_daily_cap"] = 20
    for key, value in over.items():
        policy[key] = value
    return policy


def _store(tmp):
    return StateStore(Path(tmp), SeededClock(NOW.isoformat()))


def _proposal(topic, scope="reddit", include=None, exclude=None):
    return {"schema": 1, "origin": "inferred", "interests": [
        {"topic": topic, "include": include or [], "exclude": exclude or [],
         "scope": scope, "confidence": "medium"}]}


def _planner(topic):
    return FakePlanner({"schema": 1, "queries": [topic], "scope": "reddit",
                        "semantic": True, "provenance": "model"})


def _fetcher():
    return FixtureFetcher({
        "https://www.reddit.com/search.rss": {"body": RSS},
        "https://api.github.com/search/repositories": {"body": '{"items": []}'},
    })


class Proposer:
    def __init__(self, proposal):
        self._proposal = proposal
        self.calls: list[list[str]] = []

    def propose(self, context, policy):
        self.calls.append(list(context or []))
        return json.loads(json.dumps(self._proposal))


class DeidVerifier:
    """A verifier that 'de-identifies' a safe generic topic onto the revoked one."""

    def __init__(self, safe_topic):
        self._safe = safe_topic
        self.calls = 0

    def review(self, topic, include, exclude, context, policy, *, timeout=None):
        self.calls += 1
        return {"schema": 1, "decision": "allow", "reason": "generic",
                "safe_topic": self._safe}


def _seed_and_revoke(store, topic="mycorrhizal networks"):
    collect_adaptive(_policy(), SeededClock(NOW.isoformat()), store=store,
                     planner=_planner(topic), fetcher=_fetcher(),
                     interest_proposer=Proposer(_proposal(topic)), context=CONTEXT)
    data = ip.load_inferred_registry(store)
    goal = next(iter(data["goals"].values()))
    ip.revoke_inferred_goal(data, goal["id"], NOW)
    ip.save_inferred_registry(store, data)
    return goal["id"]


class RevokedTopicTombstone(unittest.TestCase):
    def test_revoke_records_normalized_tombstone_separate_from_goals(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _seed_and_revoke(store, "MYCORRHIZAL NETWORKS")
            data = ip.load_inferred_registry(store)
            self.assertIn("mycorrhizal networks", data["revoked_topics"])
            self.assertTrue(ip.topic_is_revoked(data, "  MYCORRHIZAL   networks "))

    def test_restart_same_topic_case_variation_context_change_zero_acquisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _seed_and_revoke(store, "mycorrhizal networks")
            # Fresh StateStore on the same directory == process restart.
            restarted = StateStore(Path(tmp), SeededClock((NOW + timedelta(minutes=5)).isoformat()))
            fetcher = _fetcher()
            proposer = Proposer(_proposal("  Mycorrhizal   NETWORKS "))
            summary = collect_adaptive(
                _policy(), SeededClock((NOW + timedelta(minutes=5)).isoformat()),
                store=restarted, planner=_planner("mycorrhizal networks"), fetcher=fetcher,
                interest_proposer=proposer, context=CHANGED_CONTEXT)
            self.assertEqual(summary["status"], "no_active_watch")
            self.assertEqual(fetcher.calls, [], "revoked topic must not acquire")
            self.assertEqual(proposer.calls, [CHANGED_CONTEXT], "proposal ran once, then rejected")
            data = ip.load_inferred_registry(restarted)
            self.assertTrue(ip.topic_is_revoked(data, "mycorrhizal networks"))
            active = [g for g in data["goals"].values() if g["status"] == "active"]
            self.assertEqual(active, [], "no new goal may be registered for a revoked topic")

    def test_different_topic_allowed_after_revoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _seed_and_revoke(store, "mycorrhizal networks")
            fetcher = _fetcher()
            summary = collect_adaptive(
                _policy(), SeededClock((NOW + timedelta(minutes=5)).isoformat()),
                store=store, planner=_planner("slime mould computing"), fetcher=fetcher,
                interest_proposer=Proposer(_proposal("slime mould computing")),
                context=CHANGED_CONTEXT)
            self.assertEqual(summary["status"], "ok")
            self.assertTrue(fetcher.calls, "an unrelated safe topic must still acquire")
            data = ip.load_inferred_registry(store)
            active = [g["public_topic"] for g in data["goals"].values() if g["status"] == "active"]
            self.assertEqual(active, ["slime mould computing"])

    def test_direct_add_path_refuses_revoked_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _seed_and_revoke(store, "mycorrhizal networks")
            data = ip.load_inferred_registry(store)
            with self.assertRaises(ip.InferredGoalError):
                ip.add_inferred_goal(data, {"topic": "MYCORRHIZAL networks", "include": [],
                                            "exclude": [], "scope": "public"}, _policy(), NOW)
            # An unrelated topic is still accepted by the direct path.
            goal = ip.add_inferred_goal(data, {"topic": "slime mould computing", "include": [],
                                               "exclude": [], "scope": "public"}, _policy(), NOW)
            self.assertEqual(goal["public_topic"], "slime mould computing")

    def test_semantic_deidentified_proposal_cannot_resurrect_revoked_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _seed_and_revoke(store, "mycorrhizal networks")
            fetcher = _fetcher()
            verifier = DeidVerifier("mycorrhizal networks")
            summary = collect_adaptive(
                _policy(), SeededClock((NOW + timedelta(minutes=5)).isoformat()),
                store=store, planner=_planner("generic safe phrase"), fetcher=fetcher,
                interest_proposer=Proposer(_proposal("generic safe phrase")),
                context=CHANGED_CONTEXT, privacy_verifier=verifier)
            self.assertEqual(verifier.calls, 1, "the semantic reviewer ran")
            self.assertEqual(summary["status"], "no_active_watch")
            self.assertEqual(fetcher.calls, [], "de-identified revoked topic must not acquire")
            active = [g for g in ip.load_inferred_registry(store)["goals"].values()
                      if g["status"] == "active"]
            self.assertEqual(active, [])

    def test_tombstone_survives_goal_clear_and_ttl_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _seed_and_revoke(store, "mycorrhizal networks")
            data = ip.load_inferred_registry(store)
            data["goals"] = {}  # simulate a cap/purge that drops goal records
            data = ip.load_inferred_registry(store)
            # TTL sweep far in the future must not drop the tombstone either.
            future = NOW + timedelta(days=3650)
            ip.sweep_expired(data, future)
            ip.save_inferred_registry(store, data)
            reloaded = ip.load_inferred_registry(store)
            self.assertTrue(ip.topic_is_revoked(reloaded, "mycorrhizal networks"))
            with self.assertRaises(ip.InferredGoalError):
                ip.add_inferred_goal(reloaded, {"topic": "mycorrhizal networks", "include": [],
                                                "exclude": [], "scope": "public"}, _policy(), NOW)

    def test_duplicate_active_topic_case_whitespace_not_readded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            collect_adaptive(_policy(), SeededClock(NOW.isoformat()), store=store,
                             planner=_planner("mycorrhizal networks"), fetcher=_fetcher(),
                             interest_proposer=Proposer(_proposal("mycorrhizal networks")),
                             context=CONTEXT)
            collect_adaptive(_policy(), SeededClock((NOW + timedelta(minutes=5)).isoformat()),
                             store=store, planner=_planner("mycorrhizal networks"), fetcher=_fetcher(),
                             interest_proposer=Proposer(_proposal("Mycorrhizal   NETWORKS")),
                             context=CHANGED_CONTEXT)
            goals = ip.load_inferred_registry(store)["goals"]
            self.assertEqual(len(goals), 1, "case/whitespace duplicate must not be added")

    def test_expired_goal_is_not_a_permanent_ban(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            collect_adaptive(_policy(), SeededClock(NOW.isoformat()), store=store,
                             planner=_planner("mycorrhizal networks"), fetcher=_fetcher(),
                             interest_proposer=Proposer(_proposal("mycorrhizal networks")),
                             context=CONTEXT)
            data = ip.load_inferred_registry(store)
            old = next(iter(data["goals"].values()))
            old["expires_at"] = (NOW - timedelta(days=1)).isoformat()
            ip.save_inferred_registry(store, data)
            self.assertFalse(ip.topic_is_revoked(data, "mycorrhizal networks"))
            summary = collect_adaptive(
                _policy(), SeededClock((NOW + timedelta(minutes=5)).isoformat()),
                store=store, planner=_planner("mycorrhizal networks"), fetcher=_fetcher(),
                interest_proposer=Proposer(_proposal("mycorrhizal networks")),
                context=CHANGED_CONTEXT)
            self.assertEqual(summary["status"], "ok", "an expired goal is not a ban")
            active = [g for g in ip.load_inferred_registry(store)["goals"].values()
                      if g["status"] == "active"]
            self.assertEqual([g["public_topic"] for g in active], ["mycorrhizal networks"])

    def test_superseded_goal_is_not_a_permanent_ban(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            collect_adaptive(_policy(), SeededClock(NOW.isoformat()), store=store,
                             planner=_planner("mycorrhizal networks"), fetcher=_fetcher(),
                             interest_proposer=Proposer(_proposal("mycorrhizal networks")),
                             context=CONTEXT)
            # A changed context with a different topic supersedes the old goal.
            collect_adaptive(_policy(), SeededClock((NOW + timedelta(minutes=1)).isoformat()),
                             store=store, planner=_planner("slime mould computing"),
                             fetcher=_fetcher(),
                             interest_proposer=Proposer(_proposal("slime mould computing")),
                             context=CHANGED_CONTEXT)
            statuses = {g["public_topic"]: g["status"]
                        for g in ip.load_inferred_registry(store)["goals"].values()}
            self.assertEqual(statuses.get("mycorrhizal networks"), "superseded")
            # The superseded topic may be proposed again.
            summary = collect_adaptive(
                _policy(), SeededClock((NOW + timedelta(minutes=2)).isoformat()),
                store=store, planner=_planner("mycorrhizal networks"), fetcher=_fetcher(),
                interest_proposer=Proposer(_proposal("mycorrhizal networks")),
                context=["Plan mycorrhizal networks garden project"])
            self.assertEqual(summary["status"], "ok")
            active = {g["public_topic"]
                      for g in ip.load_inferred_registry(store)["goals"].values()
                      if g["status"] == "active"}
            self.assertIn("mycorrhizal networks", active)


if __name__ == "__main__":
    unittest.main(verbosity=2)
