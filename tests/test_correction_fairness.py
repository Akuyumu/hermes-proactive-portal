"""Correction controls: explicit-watch fairness, durable inferred revision and
paired ordinary evidence / ping-no-ping gates (offline fakes only).

Proves the parent review fix:
  * a saved explicit watch can no longer permanently starve autonomous inferred
    discovery (bounded durable fair rotation, restart-safe, many watches);
  * inferred goals are revised when the authorised context changes instead of
    retaining an unrelated cached goal forever, and stale evidence is purged;
  * ordinary packet evidence carries an honest ``origin``/``provenance`` and the
    ordinary notification gates give paired ping/no-ping outcomes for inferred
    discoveries under silence, novelty dedupe, preference, quiet, cap and pause.

No network, no model, no subprocess, no /tmp, nothing sent. Fakes are injected
for the proposer, planner, fetcher and evaluator.

  PYTHONPATH=src python3 -m unittest discover -s tests
"""
from __future__ import annotations

import copy
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
from proactive_pilot.adaptive import (  # noqa: E402
    collect_adaptive,
    load_adaptive_registry,
    prepare_adaptive_plan,
)
from proactive_pilot.candidates import build_packet  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult, FakeEvaluator  # noqa: E402
from proactive_pilot.gates import decide, record_delivery  # noqa: E402
from proactive_pilot.planner import FakePlanner  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.shadow import FixtureFetcher  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.watchstore import apply_action  # noqa: E402

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=SG)
NOVEL_CONTEXT = ["Plan mycorrhizal networks garden project", "Read about slime mould computing"]
CHANGED_CONTEXT = ["Read about slime mould computing", "Study urban lichen ecology"]

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Mycorrhizal networks advance</title>
    <link>https://example.com/x1</link>
    <description>fungal research</description>
    <pubDate>Sat, 03 Oct 2026 11:00:00 +0800</pubDate>
    <guid>x1</guid></item>
</channel></rss>"""

TOPICS = ["quantum networking", "renewable energy", "urban gardening",
          "slime mould computing", "mycorrhizal networks", "desert hydrology"]


def _policy(**over):
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    policy["_preferences"] = {"explicit": [], "inferred": [], "snoozes": [],
                              "paused": {"active": False}}
    policy["adaptive_inferred_goals_enabled"] = True
    policy["adaptive_mode"] = "active"
    for key, value in over.items():
        policy[key] = value
    return policy


def _store(tmp):
    return StateStore(Path(tmp), SeededClock(NOW.isoformat()))


def _add_watch(store, topic, scope="reddit"):
    return apply_action(store, {"action": "add", "public_topic": topic, "scope": scope,
                                "provenance": "explicit user request"}, NOW)


def _planner():
    return FakePlanner(lambda w, p: {"schema": 1, "queries": [w.get("public_topic") or "topic"],
                                     "scope": w.get("scope") or "public",
                                     "semantic": True, "provenance": "model"})


class _Proposer:
    """Deterministic fake proposer; records calls, never touches a model."""

    def __init__(self, topic):
        self.topic = topic
        self.calls = 0

    def propose(self, context, policy):
        self.calls += 1
        return {"schema": 1, "origin": "inferred", "interests": [
            {"topic": self.topic, "include": [], "exclude": [],
             "scope": "reddit", "confidence": "medium"}]}


class _EmptyProposer:
    def __init__(self):
        self.calls = 0

    def propose(self, context, policy):
        self.calls += 1
        return {"schema": 1, "origin": "inferred", "interests": []}


def _reddit_fetcher():
    return FixtureFetcher({
        "https://www.reddit.com/search.rss": {"body": RSS},
        "https://api.github.com/search/repositories": {"body": '{"items": []}'},
    })


class ExplicitFairness(unittest.TestCase):
    def test_many_explicit_watches_do_not_starve_inferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            for topic in TOPICS[:5]:
                _add_watch(store, topic)
            policy = _policy(adaptive_inferred_fairness_period=3)
            proposer = _Proposer("mycorrhizal networks")
            origins = []
            for _ in range(6):
                prepared = prepare_adaptive_plan(
                    policy, SeededClock(NOW.isoformat()), store=store,
                    planner=_planner(), context=NOVEL_CONTEXT, interest_proposer=proposer)
                origins.append(prepared.get("origin") or "explicit")
            # Explicit watches keep priority (first slots) but every 3rd slot is
            # available to inferred discovery: one in three, regardless of count.
            self.assertEqual(origins[:2], ["explicit", "explicit"])
            self.assertEqual(origins.count("inferred"), 2, origins)
            self.assertEqual(proposer.calls, 1, "an active inferred goal must be reused")

    def test_fairness_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = _policy(adaptive_inferred_fairness_period=3)
            proposer = _Proposer("mycorrhizal networks")
            _add_watch(_store(tmp), "quantum networking")
            _add_watch(_store(tmp), "renewable energy")
            origins = []
            # A fresh StateStore per iteration simulates a separate process run.
            for _ in range(3):
                store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
                prepared = prepare_adaptive_plan(
                    policy, SeededClock(NOW.isoformat()), store=store,
                    planner=_planner(), context=NOVEL_CONTEXT, interest_proposer=proposer)
                origins.append(prepared.get("origin") or "explicit")
            # Without a durable cursor every one-shot process would be explicit
            # forever. The persisted fair cursor yields the inferred slot on the
            # 3rd run instead.
            self.assertEqual(origins, ["explicit", "explicit", "inferred"])

    def test_inferred_turn_falls_back_to_explicit_when_nothing_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _add_watch(store, "quantum networking")
            policy = _policy(adaptive_inferred_fairness_period=2)
            proposer = _EmptyProposer()
            # slot 0 explicit; slot 1 is inferred-eligible but the proposal is
            # empty, so the explicit watch is still planned (never idle).
            first = prepare_adaptive_plan(
                policy, SeededClock(NOW.isoformat()), store=store,
                planner=_planner(), context=NOVEL_CONTEXT, interest_proposer=proposer)
            second = prepare_adaptive_plan(
                policy, SeededClock(NOW.isoformat()), store=store,
                planner=_planner(), context=NOVEL_CONTEXT, interest_proposer=proposer)
            self.assertEqual(first.get("origin") or "explicit", "explicit")
            self.assertEqual(second.get("origin") or "explicit", "explicit")
            self.assertTrue(second.get("watch"), "explicit watch must still be planned")
            self.assertEqual(proposer.calls, 1)


class RevisionAndDurability(unittest.TestCase):
    def _run(self, store, clock, policy, topic, context):
        return collect_adaptive(policy, clock, store=store, planner=_planner(),
                                fetcher=_reddit_fetcher(),
                                interest_proposer=_Proposer(topic), context=context)

    def test_context_change_revises_goal_and_purges_stale_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            clock = SeededClock(NOW.isoformat())
            policy = _policy(inferred_goals_cooldown_minutes=0, inferred_goals_daily_cap=10)
            fetcher = _reddit_fetcher()
            collect_adaptive(policy, clock, store=store, planner=_planner(), fetcher=fetcher,
                             interest_proposer=_Proposer("mycorrhizal networks"),
                             context=NOVEL_CONTEXT)
            data = ip.load_inferred_registry(store)
            old = next(iter(data["goals"].values()))
            old_id = old["id"]
            self.assertEqual(len(old["context_fingerprint"]), 64)
            self.assertTrue(any(str(r.get("watch_id")) == old_id
                                for r in load_adaptive_registry(store)["sources"].values()))
            marker = len(fetcher.calls)
            # Same persisted goal, DIFFERENT authorised context -> revise.
            collect_adaptive(policy, clock, store=store, planner=_planner(), fetcher=fetcher,
                             interest_proposer=_Proposer("slime mould computing"),
                             context=CHANGED_CONTEXT)
            data = ip.load_inferred_registry(store)
            self.assertEqual(data["goals"][old_id]["status"], "superseded")
            self.assertIsNone(data["goals"][old_id]["plan"])
            new = [g for g in data["goals"].values() if g["status"] == "active"]
            self.assertEqual([g["public_topic"] for g in new], ["slime mould computing"])
            # Stale evidence for the superseded goal is purged.
            self.assertFalse(any(str(r.get("watch_id")) == old_id
                                 for r in load_adaptive_registry(store)["sources"].values()))
            new_calls = fetcher.calls[marker:]
            self.assertTrue(any("slime+mould+computing" in u for u in new_calls), new_calls)

    def test_context_never_persists_raw_titles(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            collect_adaptive(_policy(), SeededClock(NOW.isoformat()), store=store,
                             planner=_planner(), fetcher=_reddit_fetcher(),
                             interest_proposer=_Proposer("mycorrhizal networks"),
                             context=["Plan mycorrhizal networks garden project"])
            raw = (Path(tmp) / ip.INFERRED_FILE).read_text(encoding="utf-8")
            self.assertNotIn("garden project", raw, "raw context titles must not persist")
            self.assertIn("context_fingerprint", raw)

    def test_cooldown_bounds_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            clock = SeededClock(NOW.isoformat())
            policy = _policy(inferred_goals_cooldown_minutes=360)
            collect_adaptive(policy, clock, store=store, planner=_planner(),
                             fetcher=_reddit_fetcher(),
                             interest_proposer=_Proposer("mycorrhizal networks"),
                             context=NOVEL_CONTEXT)
            probe = _Proposer("slime mould computing")
            summary = collect_adaptive(policy, clock, store=store, planner=_planner(),
                                       fetcher=_reddit_fetcher(), interest_proposer=probe,
                                       context=CHANGED_CONTEXT)
            # Bounded cadence: no immediate re-proposal; the ready goal is reused.
            self.assertEqual(probe.calls, 0)
            self.assertEqual(summary.get("origin"), "inferred")
            self.assertEqual(len(ip.load_inferred_registry(store)["goals"]), 1)


class PacketAndPingControls(unittest.TestCase):
    def _inferred_packet(self, tmp):
        store = _store(tmp)
        policy = _policy()
        summary = collect_adaptive(policy, SeededClock(NOW.isoformat()), store=store,
                                   planner=_planner(), fetcher=_reddit_fetcher(),
                                   interest_proposer=_Proposer("mycorrhizal networks"),
                                   context=NOVEL_CONTEXT)
        self.assertEqual(summary["status"], "ok", summary)
        results = [r for r in summary["results"] if isinstance(r, SourceResult)]
        packet = build_packet(results, clock=SeededClock(NOW.isoformat()),
                              policy=policy, baseline={})
        cand = next(c for c in packet["candidates"] if c["kind"] == "feed")
        return policy, packet, cand

    def test_inferred_candidate_origin_is_honest(self):
        with tempfile.TemporaryDirectory() as tmp:
            _policy_, packet, cand = self._inferred_packet(tmp)
            self.assertEqual(cand["origin"], "inferred")
            self.assertIn("inferred", cand["provenance"])
            self.assertNotIn("explicit user request", cand["provenance"])

    def _notify(self, cand, message="mycorrhizal update worth a look", urgency="normal", **over):
        return EvaluatorResult("notify", kind="feed", key=cand["ref"], message=message,
                               urgency=urgency, evidence=[cand["ref"]],
                               consequence=over.get("consequence", ""),
                               confidence=over.get("confidence", ""),
                               deadline=over.get("deadline", ""))

    def test_ping_then_duplicate_novelty_no_ping(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, packet, cand = self._inferred_packet(tmp)
            clock = SeededClock(NOW.isoformat())
            prefs = {"explicit": [], "snoozes": [], "paused": {"active": False}}
            policy["cooldown_minutes"] = 0
            prop = FakeEvaluator(self._notify(cand)).evaluate(packet, policy)
            state = {}
            first = decide(prop, packet, clock=clock, policy=policy, prefs=prefs, state=state)
            self.assertTrue(first["send"], first)
            record_delivery(state, first, packet, clock, policy)
            dup = decide(prop, packet, clock=clock, policy=policy, prefs=prefs, state=state)
            self.assertFalse(dup["send"])
            self.assertEqual(dup["reason"], "duplicate")
            # Novelty: a changed source version is a genuinely distinct event.
            packet2 = copy.deepcopy(packet)
            for c in packet2["candidates"]:
                if c["ref"] == cand["ref"]:
                    c["version"] = "new-version"
            fresh = decide(prop, packet2, clock=clock, policy=policy, prefs=prefs, state=state)
            self.assertTrue(fresh["send"], fresh)

    def test_silence_is_no_ping(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, packet, cand = self._inferred_packet(tmp)
            prop = FakeEvaluator(EvaluatorResult("silence", reason="not useful")).evaluate(packet, policy)
            out = decide(prop, packet, clock=SeededClock(NOW.isoformat()), policy=policy,
                         prefs={"explicit": [], "snoozes": [], "paused": {"active": False}}, state={})
            self.assertFalse(out["send"])
            self.assertEqual(out["reason"], "silence")

    def test_envelope_gates_pair_ping_and_no_ping(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, packet, cand = self._inferred_packet(tmp)
            clock = SeededClock(NOW.isoformat())
            notify = self._notify(cand)
            base_prefs = {"explicit": [], "snoozes": [], "paused": {"active": False}}

            # Quiet hours block the ping.
            quiet = dict(policy, quiet_start_hour=0, quiet_end_hour=13)
            out = decide(notify, packet, clock=clock, policy=quiet, prefs=base_prefs, state={})
            self.assertFalse(out["send"])
            self.assertEqual(out["reason"], "quiet hours")

            # Explicit preference blocks the ping.
            prefs = {"explicit": [{"id": "p1", "kind": "feed", "match": "mycorrhizal",
                                   "suppress": True, "conditional": False, "provenance": "user"}],
                     "snoozes": [], "paused": {"active": False}}
            out = decide(notify, packet, clock=clock, policy=policy, prefs=prefs, state={})
            self.assertFalse(out["send"])
            self.assertEqual(out["reason"], "explicit preference")

            # Global pause/snooze blocks the ping.
            paused = {"explicit": [], "snoozes": [], "paused": {"active": True}}
            out = decide(notify, packet, clock=clock, policy=policy, prefs=paused, state={})
            self.assertFalse(out["send"])
            self.assertEqual(out["reason"], "snoozed")

            # Daily cap reached blocks the ping.
            capped = {"delivered": [{"at": NOW.isoformat()} for _ in range(3)]}
            out = decide(notify, packet, clock=clock, policy=dict(policy, daily_normal_cap=3),
                         prefs=base_prefs, state=capped)
            self.assertFalse(out["send"])
            self.assertEqual(out["reason"], "daily cap")

            # Cooldown blocks the ping.
            cooling = {"last_delivery_at": NOW.isoformat()}
            out = decide(notify, packet, clock=clock, policy=dict(policy, cooldown_minutes=30),
                         prefs=base_prefs, state=cooling)
            self.assertFalse(out["send"])
            self.assertEqual(out["reason"], "cooldown")

    def test_urgent_inferred_feed_is_never_urgent(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy, packet, cand = self._inferred_packet(tmp)
            prop = self._notify(cand, urgency="urgent", consequence="feels urgent",
                                confidence="high",
                                deadline=(NOW + timedelta(minutes=30)).isoformat())
            out = decide(prop, packet, clock=SeededClock(NOW.isoformat()), policy=policy,
                         prefs={"explicit": [], "snoozes": [], "paused": {"active": False}}, state={})
            self.assertFalse(out["send"])
            self.assertEqual(out["reason"], "feed item never urgent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
