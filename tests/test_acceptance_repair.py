"""Acceptance repair controls (offline fakes only; no network/model/send).

Covers the six ACCEPTANCE_REPAIR.md blockers:
  1. explicit suppression/snooze/conditional semantics on EVERY cached/fallback
     inferred selection, with blocked evidence purged but the goal NOT revoked;
  2. shared proposer+resolver+planner budget computed and clamped BEFORE any
     model call, with a reserved public/obligation allocation and no reset;
  3. an enforced structural privacy boundary before external search plus a
     separate injectable zero-tool semantic verifier that fails closed;
  4. truthful explicit/inferred origin in the planner prompt and plan;
  5. a genuinely keyed (owner-only random salt) context fingerprint;
  6. all genuine proposal attempts count against the bounded cap + backoff, and a
     rejected/empty revision cannot mark a changed context handled forever.

  PYTHONPATH=src python3 -m unittest discover -s tests
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
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
    prepare_inferred_plan,
)
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.planner import FakePlanner, NativePlanner, validate_plan  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.shadow import FixtureFetcher  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

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


def _proposal(topic="mycorrhizal networks", scope="reddit", include=None, exclude=None):
    return {"schema": 1, "origin": "inferred", "interests": [
        {"topic": topic, "include": include or [], "exclude": exclude or [],
         "scope": scope, "confidence": "medium"}]}


class _Proposer:
    def __init__(self, proposal):
        self._proposal = proposal
        self.calls = 0

    def propose(self, context, policy):
        self.calls += 1
        result = self._proposal(context, policy) if callable(self._proposal) else self._proposal
        if isinstance(result, Exception):
            raise result
        return json.loads(json.dumps(result))


class _Planner:
    def __init__(self, result=None):
        self.calls = 0
        self._result = result

    def plan(self, watch, policy):
        self.calls += 1
        topic = str(watch.get("public_topic") or "x")
        return self._result or {"schema": 1, "queries": [topic], "scope": "reddit",
                                "semantic": True, "provenance": "model"}


def _fetcher():
    return FixtureFetcher({
        "https://www.reddit.com/search.rss": {"body": RSS},
        "https://api.github.com/search/repositories": {"body": '{"items": []}'},
    })


def _seed_goal(store, topic="mycorrhizal networks", include=None):
    policy = _policy(inferred_goals_cooldown_minutes=0, inferred_goals_daily_cap=20)
    collect_adaptive(policy, SeededClock(NOW.isoformat()), store=store, planner=_Planner(),
                     fetcher=_fetcher(),
                     interest_proposer=_Proposer(_proposal(topic, include=include)),
                     context=NOVEL_CONTEXT)
    return policy


# --------------------------------------------------------------------------- #
# Blocker 1: preferences/snoozes on cached selection + evidence purge
# --------------------------------------------------------------------------- #


class PreferenceOnCachedSelection(unittest.TestCase):
    def test_blocked_cached_goal_purges_evidence_without_revoking(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _seed_goal(store, include=["fungal"])
            goal = next(iter(ip.load_inferred_registry(store)["goals"].values()))
            gid = goal["id"]
            self.assertTrue(any(str(r.get("watch_id")) == gid
                                for r in load_adaptive_registry(store)["sources"].values()))
            policy = _policy(inferred_goals_cooldown_minutes=0)
            policy["_preferences"] = {"explicit": [
                {"id": "p1", "kind": "feed", "match": "fungal", "suppress": True,
                 "conditional": False, "provenance": "user"}]}
            fetcher = _fetcher()
            proposer = _Proposer(_proposal("slime mould computing"))
            summary = collect_adaptive(policy, SeededClock(NOW.isoformat()), store=store,
                                       planner=_Planner(), fetcher=fetcher,
                                       interest_proposer=proposer, context=NOVEL_CONTEXT)
            # Zero new model/search/fetch on a cached but suppressed goal.
            self.assertEqual(proposer.calls, 0)
            self.assertEqual(fetcher.calls, [])
            self.assertEqual(summary["status"], "no_active_watch")
            data = ip.load_inferred_registry(store)
            self.assertEqual(data["goals"][gid]["status"], "active", "snooze must NOT revoke")
            self.assertTrue(data["goals"][gid]["preference_blocked"])
            # Cached evidence purged (include-term match is honoured, not just topic).
            self.assertFalse(any(str(r.get("watch_id")) == gid
                                 for r in load_adaptive_registry(store)["sources"].values()))

    def test_reallow_after_block_resumes_acquisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            policy = _seed_goal(store)
            goal = next(iter(ip.load_inferred_registry(store)["goals"].values()))
            policy["_preferences"] = {"explicit": [
                {"id": "p1", "kind": "feed", "match": "mycorrhizal", "suppress": True,
                 "conditional": False, "provenance": "user"}]}
            collect_adaptive(policy, SeededClock(NOW.isoformat()), store=store,
                             planner=_Planner(), fetcher=_fetcher(),
                             interest_proposer=_Proposer(_proposal("x")), context=NOVEL_CONTEXT)
            # Re-allow (fresh state store simulates restart): the still-active goal
            # is selected again and re-acquires.
            policy["_preferences"] = {"explicit": [], "snoozes": [], "paused": {"active": False}}
            fetcher = _fetcher()
            proposer = _Proposer(_proposal("x"))
            collect_adaptive(policy, SeededClock(NOW.isoformat()), store=store,
                             planner=_Planner(), fetcher=fetcher,
                             interest_proposer=proposer, context=NOVEL_CONTEXT)
            self.assertEqual(proposer.calls, 0, "reused cached goal")
            self.assertTrue(fetcher.calls, "re-allowed goal must re-acquire")
            self.assertEqual(ip.load_inferred_registry(store)["goals"][goal["id"]]["status"],
                             "active")

    def test_conditional_exception_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            policy = _seed_goal(store)
            policy["_preferences"] = {"explicit": [
                {"id": "p1", "kind": "feed", "match": "mycorrhizal", "suppress": True,
                 "conditional": True, "provenance": "career noise exception"}]}
            fetcher = _fetcher()
            later = (NOW + timedelta(days=1)).isoformat()
            collect_adaptive(policy, SeededClock(later), store=store,
                             planner=_Planner(), fetcher=fetcher,
                             interest_proposer=_Proposer(_proposal("x")), context=NOVEL_CONTEXT)
            self.assertTrue(fetcher.calls, "conditional rule must never blanket-suppress")


# --------------------------------------------------------------------------- #
# Blocker 2: shared total proposer + planner budget
# --------------------------------------------------------------------------- #


class _FakeBudget:
    def __init__(self, remaining):
        self._remaining = float(remaining)

    def remaining(self):
        return self._remaining

    def take(self, want):
        return int(want)


class SharedStageBudget(unittest.TestCase):
    def test_near_zero_fit_makes_zero_proposer_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _seed_goal(store)
            policy = _policy()
            proposer = _Proposer(_proposal("slime mould computing"))
            planner = _Planner()
            # remaining below the 10+15 reservation -> no model proposal.
            result = prepare_inferred_plan(
                policy, SeededClock(NOW.isoformat()), store=store, planner=planner,
                context=NOVEL_CONTEXT, interest_proposer=proposer,
                overall_budget=_FakeBudget(20.0))
            self.assertIsNotNone(result, "cache reuse is safe with zero budget")
            self.assertEqual(proposer.calls, 0)
            self.assertEqual(planner.calls, 0)
            self.assertFalse(result["used_model"])
            self.assertTrue(result["planning_deferred"])

    def test_stage_deadline_reflects_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            policy = _policy()
            seen = {}

            def proposal(context, p):
                seen["deadline"] = p.get("_inferred_stage_deadline")
                return _proposal("mycorrhizal networks")

            before = time.monotonic()
            result = prepare_inferred_plan(
                policy, SeededClock(NOW.isoformat()), store=store, planner=_Planner(),
                context=NOVEL_CONTEXT, interest_proposer=_Proposer(proposal),
                overall_budget=_FakeBudget(30.0))
            self.assertIsNotNone(result)
            # fit = 30 - (public 10 + obligation 15) = 5s of shared stage time.
            self.assertIsNotNone(seen.get("deadline"))
            self.assertLessEqual(seen["deadline"] - before, 5.25)

    def test_native_proposer_clamps_and_fails_closed_when_exhausted(self):
        policy = _policy()
        policy["_inferred_stage_deadline"] = time.monotonic() - 1.0
        calls = {"resolver": 0, "runner": 0}

        def resolver(*a, **k):
            calls["resolver"] += 1
            return True, [], "x"

        def runner(*a, **k):
            calls["runner"] += 1
            return 0, "{}", ""

        proposer = ip.NativeInterestProposer(resolver=resolver, runner=runner)
        with self.assertRaises(ip.InferredProposalError):
            proposer.propose(NOVEL_CONTEXT, policy)
        self.assertEqual(calls, {"resolver": 0, "runner": 0},
                         "an exhausted stage budget must make ZERO calls")

    def test_exact_baseline_envelope_unchanged(self):
        policy = _policy()
        self.assertEqual(policy["public_budget_seconds"], 10)
        self.assertEqual(policy["core_budget_seconds"], 35)
        self.assertEqual(policy["obligation_budget_seconds"], 15)
        self.assertEqual(policy["collector_budget_seconds"], 60)


# --------------------------------------------------------------------------- #
# Blocker 3: enforced privacy boundary + fail-closed semantic verifier
# --------------------------------------------------------------------------- #


class _Verifier:
    def __init__(self, result):
        self._result = result
        self.calls = 0

    def review(self, topic, include, exclude, context, policy, *, timeout=None):
        self.calls += 1
        return self._result(topic, context) if callable(self._result) else self._result


class PrivacyBoundary(unittest.TestCase):
    def test_structural_rejects_names_and_private_codenames(self):
        for bad in ("Alice Johnson", "Project Nimbus", "ZephyrX deployment",
                    "meet Professor Green"):
            with self.assertRaises(ip.InferredGoalError, msg=bad):
                ip.validate_proposal(_proposal(bad), _policy())

    def test_copied_context_phrase_rejected_even_from_lowercase_proposer(self):
        context = ["Meeting with Alice Johnson about Project Nimbus"]
        with self.assertRaises(ip.InferredGoalError):
            ip.verify_interest(
                {"topic": "alice johnson", "include": [], "exclude": [],
                 "scope": "reddit", "query": "alice johnson"},
                context, _policy())

    def test_name_and_private_event_zero_search_fetch(self):
        for bad_topic in ("Alice Johnson", "Project Nimbus"):
            with tempfile.TemporaryDirectory() as tmp:
                store = _store(tmp)
                fetcher = _fetcher()
                resolver_calls = {"n": 0}

                def resolver(*a, **k):
                    resolver_calls["n"] += 1
                    return ["93.184.216.34"]

                summary = collect_adaptive(
                    _policy(adaptive_broad_discovery_enabled=True),
                    SeededClock(NOW.isoformat()), store=store, planner=_Planner(),
                    fetcher=fetcher,
                    interest_proposer=_Proposer(_proposal(bad_topic)),
                    context=["Lunch with Alice Johnson", "Project Nimbus kickoff"],
                    resolver=resolver)
                self.assertEqual(summary["status"], "no_active_watch")
                self.assertEqual(fetcher.calls, [])
                self.assertEqual(resolver_calls["n"], 0)
                self.assertEqual(ip.load_inferred_registry(store)["goals"], {})

    def test_generic_biological_concept_reachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            fetcher = _fetcher()
            summary = collect_adaptive(_policy(), SeededClock(NOW.isoformat()), store=store,
                                       planner=_Planner(), fetcher=fetcher,
                                       interest_proposer=_Proposer(_proposal("mycorrhizal networks")),
                                       context=NOVEL_CONTEXT)
            self.assertEqual(summary["status"], "ok")
            self.assertTrue(fetcher.calls)

    def _run_with_verifier(self, verifier):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            fetcher = _fetcher()
            summary = collect_adaptive(_policy(), SeededClock(NOW.isoformat()), store=store,
                                       planner=_Planner(), fetcher=fetcher,
                                       interest_proposer=_Proposer(_proposal("mycorrhizal networks")),
                                       context=NOVEL_CONTEXT, privacy_verifier=verifier)
            return summary, fetcher, ip.load_inferred_registry(store)

    def test_verifier_unavailable_fails_closed(self):
        class Boom:
            def review(self, *a, **k):
                raise RuntimeError("model down")

        class TimedOut:
            def review(self, *a, **k):
                raise TimeoutError("model timeout")

        for verifier in (Boom(), TimedOut()):
            summary, fetcher, data = self._run_with_verifier(verifier)
            self.assertEqual(summary["status"], "no_active_watch")
            self.assertEqual(fetcher.calls, [])
            self.assertEqual(data["goals"], {})

    def test_verifier_malformed_and_timeout_fail_closed(self):
        for bad in ({"foo": 1}, {"schema": 1, "decision": "maybe"},
                    "not-a-dict"):
            summary, fetcher, data = self._run_with_verifier(_Verifier(bad))
            self.assertEqual(data["goals"], {}, bad)
            self.assertEqual(fetcher.calls, [], bad)

    def test_verifier_reject_and_unsafe_replacement_fail_closed(self):
        reject = {"schema": 1, "decision": "reject", "reason": "uncertain"}
        summary, fetcher, data = self._run_with_verifier(_Verifier(reject))
        self.assertEqual(data["goals"], {})
        # A "de-identified" replacement that is actually unsafe is rejected.
        unsafe = {"schema": 1, "decision": "allow", "safe_topic": "Alice Johnson"}
        summary, fetcher, data = self._run_with_verifier(_Verifier(unsafe))
        self.assertEqual(data["goals"], {})

    def test_verifier_cannot_output_arbitrary_query_or_permissions(self):
        tampered = {"schema": 1, "decision": "allow", "safe_topic": "",
                    "query": "evil.example", "scope": "github",
                    "permissions": ["send"], "authority": "user"}
        summary, fetcher, data = self._run_with_verifier(_Verifier(tampered))
        self.assertEqual(summary["status"], "ok")
        goal = next(iter(data["goals"].values()))
        self.assertEqual(goal["scope"], "reddit", "verifier must not change scope")
        self.assertNotIn("evil.example", goal["public_topic"])
        self.assertNotIn("permissions", goal)


# --------------------------------------------------------------------------- #
# Blocker 4: truthful planner provenance
# --------------------------------------------------------------------------- #


class PlannerProvenance(unittest.TestCase):
    def test_inferred_prompt_is_honest_and_not_user_stated(self):
        captured = {}

        def runner(argv, prompt, timeout):
            captured["prompt"] = prompt
            return 0, json.dumps({"schema": 1, "queries": ["mycorrhizal networks"],
                                  "scope": "reddit", "semantic": True,
                                  "provenance": "model"}), ""

        planner = NativePlanner(resolver=lambda *a, **k: (True, [], "x"), runner=runner)
        watch = {"id": "ig_1", "version": 1, "public_topic": "mycorrhizal networks",
                 "scope": "reddit", "include": [], "exclude": [], "origin": "inferred"}
        plan = planner.plan(watch, _policy())
        self.assertEqual(plan["origin"], "inferred")
        self.assertTrue(plan["advisory"])
        self.assertIn("inferred", captured["prompt"].lower())
        self.assertIn("NOT a user request", captured["prompt"])
        self.assertIn("advisory", captured["prompt"].lower())

    def test_explicit_prompt_preserves_user_stated_semantics(self):
        captured = {}

        def runner(argv, prompt, timeout):
            captured["prompt"] = prompt
            return 0, json.dumps({"schema": 1, "queries": ["quantum networking"],
                                  "scope": "reddit", "semantic": True,
                                  "provenance": "model"}), ""

        planner = NativePlanner(resolver=lambda *a, **k: (True, [], "x"), runner=runner)
        watch = {"id": "w_1", "version": 1, "public_topic": "quantum networking",
                 "scope": "reddit", "include": ["fusion"], "exclude": ["toy"]}
        plan = planner.plan(watch, _policy())
        self.assertEqual(plan["origin"], "explicit")
        self.assertFalse(plan["advisory"])
        self.assertIn("user-stated", captured["prompt"])
        self.assertIn("EXPLICIT", captured["prompt"])

    def test_validate_plan_records_origin_from_watch_not_model(self):
        watch = {"id": "ig_1", "version": 1, "scope": "public", "origin": "inferred"}
        plan = validate_plan({"schema": 1, "queries": ["x"], "scope": "public",
                              "origin": "explicit", "provenance": "model"},
                             _policy(), watch=watch, now=NOW)
        self.assertEqual(plan["origin"], "inferred", "model cannot claim explicit origin")


# --------------------------------------------------------------------------- #
# Blocker 5: genuinely keyed context fingerprint
# --------------------------------------------------------------------------- #


class KeyedFingerprint(unittest.TestCase):
    def test_registry_stores_owner_only_salt_and_keyed_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _seed_goal(store)
            data = ip.load_inferred_registry(store)
            salt = str(data.get("fingerprint_salt") or "")
            self.assertEqual(len(salt), 64)
            self.assertEqual(len(next(iter(data["goals"].values()))["context_fingerprint"]), 64)

    def test_same_context_different_salt_differs(self):
        ctx = list(NOVEL_CONTEXT)
        a = ip._context_fingerprint(ctx, "a" * 64)
        b = ip._context_fingerprint(ctx, "b" * 64)
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 64)


# --------------------------------------------------------------------------- #
# Blocker 6: attempt caps/backoff + honest context revision
# --------------------------------------------------------------------------- #


class AttemptEnvelope(unittest.TestCase):
    def test_failed_proposals_consume_daily_cap_and_arm_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            policy = _policy(inferred_goals_daily_cap=5, inferred_goals_cooldown_minutes=0,
                             inferred_proposal_failure_threshold=2,
                             inferred_proposal_backoff_minutes=360)
            failing = _Proposer(RuntimeError("model down"))
            collect_adaptive(policy, SeededClock(NOW.isoformat()), store=store,
                             planner=_Planner(), fetcher=_fetcher(),
                             interest_proposer=failing, context=NOVEL_CONTEXT)
            collect_adaptive(policy, SeededClock(NOW.isoformat()), store=store,
                             planner=_Planner(), fetcher=_fetcher(),
                             interest_proposer=failing, context=NOVEL_CONTEXT)
            data = ip.load_inferred_registry(store)
            self.assertEqual(data["proposal_failures"], 2)
            self.assertGreater(ip.proposals_today(data, NOW), 0,
                               "failed attempts must consume the daily attempt cap")
            self.assertNotEqual(data["proposal_backoff_until"], "")
            # Backoff stops any further model call, even with a good proposer.
            good = _Proposer(_proposal("mycorrhizal networks"))
            collect_adaptive(policy, SeededClock(NOW.isoformat()), store=store,
                             planner=_Planner(), fetcher=_fetcher(),
                             interest_proposer=good, context=NOVEL_CONTEXT)
            self.assertEqual(good.calls, 0, "failure backoff must stop further proposals")

    def test_changed_context_empty_then_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            policy = _seed_goal(store)
            old = next(iter(ip.load_inferred_registry(store)["goals"].values()))
            old_id = old["id"]
            old_fp = str(ip.load_inferred_registry(store)["context_fingerprint"])
            # Changed context but an EMPTY proposal: the old goal is retained and
            # the context revision must NOT be marked handled.
            collect_adaptive(policy, SeededClock(NOW.isoformat()), store=store,
                             planner=_Planner(), fetcher=_fetcher(),
                             interest_proposer=_Proposer({"schema": 1, "origin": "inferred",
                                                          "interests": []}),
                             context=CHANGED_CONTEXT)
            data = ip.load_inferred_registry(store)
            self.assertEqual(data["goals"][old_id]["status"], "active")
            self.assertEqual(str(data["context_fingerprint"]), old_fp,
                             "an all-rejected revision must not mark the change handled")
            # A later successful revision recovers and supersedes the old goal.
            collect_adaptive(policy, SeededClock(NOW.isoformat()), store=store,
                             planner=_Planner(), fetcher=_fetcher(),
                             interest_proposer=_Proposer(_proposal("slime mould computing")),
                             context=CHANGED_CONTEXT)
            data = ip.load_inferred_registry(store)
            self.assertEqual(data["goals"][old_id]["status"], "superseded")
            self.assertNotEqual(str(data["context_fingerprint"]), old_fp)
            active = [g for g in data["goals"].values() if g["status"] == "active"]
            self.assertEqual([g["public_topic"] for g in active], ["slime mould computing"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
