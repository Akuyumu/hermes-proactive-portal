"""Corrective regressions for the parent review (stage-only, fake only).

Every test injects a fake planner/fetcher and uses a temp state dir. No network,
no private/live source, no production model, no message is sent. Covers:

 1. watch_lock serialises collection and preserves intervening user edits;
    malformed persisted watch records fail closed.
 2. planning and HTTP budgets are separate; a slow planner cannot starve the
    first fetch; fetch timeouts are clamped to the remaining HTTP budget.
 3. strict plan schema/type validation and watch-authority scope honouring
    (github is never silently retargeted; news/general web stay unsupported).
 4. a criteria-only revision changes the next acquisition even in the
    deterministic fallback.
 5. retirement (stop/expiry) purges obsolete adaptive evidence.
 6. the disabled-by-default production hook yields real candidates only when
    explicitly enabled and drops retired candidates when the watch changes.
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

from proactive_pilot.adaptive import (  # noqa: E402
    build_packet_results,
    collect_adaptive,
    ensure_adaptive_sources,
    load_adaptive_registry,
    load_coverage,
    new_adaptive_registry,
)
from proactive_pilot.candidates import build_packet  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import Budget  # noqa: E402
from proactive_pilot.main import collect_public_and_adaptive  # noqa: E402
from proactive_pilot.planner import (  # noqa: E402
    FakePlanner,
    PlannerError,
    deterministic_plan,
    plan_for_watch,
    validate_plan,
)
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.shadow import FixtureFetcher  # noqa: E402
from proactive_pilot.state import CorruptStateError, StateStore  # noqa: E402
from proactive_pilot.watchstore import apply_action, load_watchstore, watch_lock  # noqa: E402

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


def _policy(**over):
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    policy["_preferences"] = {"explicit": [], "inferred": [], "snoozes": []}
    policy.update(over)
    return policy


def _store(tmp):
    return StateStore(Path(tmp), SeededClock(NOW.isoformat()))


def _add(store, topic, **over):
    action = {"action": "add", "public_topic": topic, "scope": "reddit",
              "provenance": "explicit user request"}
    action.update(over)
    return apply_action(store, action, NOW)


def _plan(query="quantum networking", scope="reddit", **over):
    plan = {"schema": 1, "queries": [query], "scope": scope,
            "semantic": True, "provenance": "model"}
    plan.update(over)
    return plan


class RaisingPlanner:
    def plan(self, watch, policy):
        raise PlannerError("no model in test")


class RecordingTimeoutFetcher(FixtureFetcher):
    def __init__(self, responses=None):
        super().__init__(responses or {})
        self.timeouts: list[int] = []

    def __call__(self, url, **kwargs):
        self.timeouts.append(int(kwargs.get("timeout", 0)))
        return super().__call__(url, **kwargs)


class WatchLockTests(unittest.TestCase):
    def test_collection_busy_while_user_holds_watch_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _add(store, "quantum networking")
            planner = FakePlanner(_plan())
            fetcher = FixtureFetcher({"https://www.reddit.com/search.rss": {"body": RSS}})
            with watch_lock(store) as held:
                self.assertTrue(held)
                summary = collect_adaptive(_policy(), SeededClock(NOW.isoformat()),
                                           store=store, planner=planner, fetcher=fetcher)
            self.assertEqual(summary["status"], "busy")
            self.assertEqual(planner.calls, [])
            self.assertEqual(fetcher.calls, [])

    def test_intervening_user_edit_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            watch = _add(store, "quantum networking")
            clock = SeededClock(NOW.isoformat())
            planner = FakePlanner(lambda w, p: _plan(w["public_topic"]))
            fetcher = FixtureFetcher({"https://www.reddit.com/search.rss": {"body": RSS}})
            collect_adaptive(_policy(), clock, store=store, planner=planner, fetcher=fetcher)
            apply_action(store, {"action": "change", "id": watch["id"],
                                 "public_topic": "renewable energy",
                                 "provenance": "explicit user request"}, NOW)
            collect_adaptive(_policy(), clock, store=store, planner=planner, fetcher=fetcher)
            data = load_watchstore(store, strict=True)
            self.assertEqual(data["watches"][watch["id"]]["public_topic"], "renewable energy")
            self.assertEqual(data["watches"][watch["id"]]["version"], 2)

    def test_malformed_persisted_watch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _add(store, "quantum networking")
            path = Path(tmp) / "adaptive_watches.json"
            data = json.loads(path.read_text())
            wid = next(iter(data["watches"]))
            data["watches"][wid]["created_at"] = "not-a-datetime"
            path.write_text(json.dumps(data))
            with self.assertRaises(CorruptStateError):
                collect_adaptive(_policy(), SeededClock(NOW.isoformat()), store=store,
                                 planner=FakePlanner(_plan()),
                                 fetcher=FixtureFetcher({}))


class BudgetSplitTests(unittest.TestCase):
    def test_slow_planner_does_not_starve_first_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _add(store, "quantum networking")

            def slow(watch, policy):
                time.sleep(0.3)
                return _plan()

            fetcher = FixtureFetcher({"https://www.reddit.com/search.rss": {"body": RSS}})
            summary = collect_adaptive(_policy(public_budget_seconds=2), SeededClock(NOW.isoformat()),
                                       store=store, planner=FakePlanner(slow), fetcher=fetcher,
                                       budget=Budget(2), restart_budget=True)
            self.assertEqual(summary["status"], "ok")
            self.assertTrue(fetcher.calls, "first fake fetch must still run within the HTTP budget")

    def test_fetch_timeout_clamped_to_remaining_http_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _add(store, "quantum networking")
            fetcher = RecordingTimeoutFetcher({"https://www.reddit.com/search.rss": {"body": RSS}})
            policy = _policy(public_budget_seconds=10, feed_timeout_seconds=15)
            summary = collect_adaptive(policy, SeededClock(NOW.isoformat()), store=store,
                                       planner=FakePlanner(_plan()), fetcher=fetcher,
                                       budget=Budget(10), restart_budget=True)
            self.assertEqual(summary["status"], "ok")
            self.assertTrue(fetcher.timeouts)
            self.assertLessEqual(max(fetcher.timeouts), 10,
                                 "fetch timeout must be bounded by the HTTP budget, not 15")

    def test_paused_zero_calls_even_with_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _add(store, "quantum networking")
            planner = FakePlanner(_plan())
            fetcher = FixtureFetcher({"https://www.reddit.com/search.rss": {"body": RSS}})
            summary = collect_adaptive(_policy(), SeededClock(NOW.isoformat()), store=store,
                                       planner=planner, fetcher=fetcher, paused=True,
                                       budget=Budget(10), restart_budget=True)
            self.assertEqual(summary["status"], "skipped")
            self.assertEqual(fetcher.calls, [])


class PlanStrictnessTests(unittest.TestCase):
    def test_missing_schema_rejected(self):
        with self.assertRaises(PlannerError):
            validate_plan({"queries": ["ok"], "semantic": True}, _policy())

    def test_numeric_query_rejected(self):
        with self.assertRaises(PlannerError):
            validate_plan({"schema": 1, "queries": [123]}, _policy())

    def test_string_semantic_rejected(self):
        with self.assertRaises(PlannerError):
            validate_plan({"schema": 1, "queries": ["ok"], "semantic": "yes"}, _policy())

    def test_watch_authority_scope_wins_over_model(self):
        watch = {"id": "w1", "version": 1, "scope": "github", "public_topic": "quantum"}
        plan = validate_plan(_plan(query="quantum", scope="public"), _policy(), watch=watch)
        self.assertEqual(plan["scope"], "github")
        self.assertEqual(plan["proposed_scope"], "public")

    def test_github_watch_only_fetches_github(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _add(store, "quantum networking", scope="github")
            planner = FakePlanner(_plan(query="quantum networking", scope="public"))
            fetcher = FixtureFetcher({"https://api.github.com/search/repositories": {"body": json.dumps({"items": []})}})
            collect_adaptive(_policy(), SeededClock(NOW.isoformat()), store=store,
                             planner=planner, fetcher=fetcher)
            self.assertTrue(fetcher.calls)
            self.assertTrue(all(u.startswith("https://api.github.com/") for u in fetcher.calls))
            self.assertFalse(any("reddit.com" in u for u in fetcher.calls))

    def test_model_scope_cannot_rescue_unsupported_watch(self):
        watch = {"id": "w1", "version": 1, "scope": "scholarship", "public_topic": "quantum"}
        plan = validate_plan(_plan(query="quantum", scope="public"), _policy(), watch=watch)
        self.assertEqual(plan["scope"], "scholarship")


class CriteriaRevisionTests(unittest.TestCase):
    def test_deterministic_fallback_reflects_include_and_exclude(self):
        watch = {"id": "w1", "version": 1, "scope": "public",
                 "public_topic": "quantum networking",
                 "include": ["research"], "exclude": ["recruitment"]}
        plan = deterministic_plan(watch, _policy(), now=NOW)
        self.assertIn("research", plan["queries"][0])
        self.assertIn("-recruitment", plan["queries"][0])
        changed = dict(watch, version=2, include=["conference"])
        plan2 = deterministic_plan(changed, _policy(), now=NOW)
        self.assertNotEqual(plan["queries"], plan2["queries"])
        self.assertIn("conference", plan2["queries"][0])

    def test_criteria_only_change_changes_next_fetch_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            watch = _add(store, "quantum networking", include=["research"])
            fetcher = FixtureFetcher({"https://www.reddit.com/search.rss": {"body": RSS}})
            clock = SeededClock(NOW.isoformat())
            collect_adaptive(_policy(), clock, store=store, planner=RaisingPlanner(), fetcher=fetcher)
            self.assertTrue(any("research" in u for u in fetcher.calls))
            marker = len(fetcher.calls)
            apply_action(store, {"action": "change", "id": watch["id"],
                                 "include": ["conference"],
                                 "provenance": "explicit user request"}, NOW)
            collect_adaptive(_policy(), clock, store=store, planner=RaisingPlanner(), fetcher=fetcher)
            new_calls = fetcher.calls[marker:]
            self.assertTrue(any("conference" in u for u in new_calls))
            self.assertFalse(any("research" in u for u in new_calls))


class RetirementTests(unittest.TestCase):
    def _seed_sources(self, store, watch):
        registry = new_adaptive_registry()
        ensure_adaptive_sources(registry, {"watch_id": watch["id"], "watch_version": 1,
                                           "scope": "reddit", "queries": ["quantum"],
                                           "semantic": True, "provenance": "model"},
                                _policy(), NOW)
        self.assertTrue(registry["sources"])
        from proactive_pilot.adaptive import save_adaptive_registry
        save_adaptive_registry(store, registry)

    def test_stop_purges_evidence_and_no_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            watch = _add(store, "quantum networking")
            self._seed_sources(store, watch)
            apply_action(store, {"action": "stop", "id": watch["id"]}, NOW)
            registry = load_adaptive_registry(store)
            self.assertFalse([r for r in registry["sources"].values()
                              if r.get("watch_id") == watch["id"]])
            fetcher = FixtureFetcher({"https://www.reddit.com/search.rss": {"body": RSS}})
            summary = collect_adaptive(_policy(), SeededClock(NOW.isoformat()), store=store,
                                       planner=FakePlanner(_plan()), fetcher=fetcher)
            self.assertEqual(summary["status"], "no_active_watch")
            self.assertEqual(fetcher.calls, [])

    def test_expiry_purges_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            watch = _add(store, "quantum networking",
                         expires_at=(NOW + timedelta(hours=1)).isoformat())
            self._seed_sources(store, watch)
            later = SeededClock((NOW + timedelta(hours=2)).isoformat())
            collect_adaptive(_policy(), later, store=store,
                             planner=FakePlanner(_plan()), fetcher=FixtureFetcher({}))
            registry = load_adaptive_registry(store)
            self.assertFalse([r for r in registry["sources"].values()
                              if r.get("watch_id") == watch["id"]])

    def test_forget_purges_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            watch = _add(store, "quantum networking")
            fetcher = FixtureFetcher({"https://www.reddit.com/search.rss": {"body": RSS}})
            collect_adaptive(_policy(), SeededClock(NOW.isoformat()), store=store,
                             planner=FakePlanner(_plan()), fetcher=fetcher)
            self.assertIn(watch["id"], load_coverage(store)["watches"])
            apply_action(store, {"action": "forget", "id": watch["id"]}, NOW)
            self.assertNotIn(watch["id"], load_coverage(store)["watches"])


class ProductionHookTests(unittest.TestCase):
    def _clock(self):
        class C:
            def now(self):
                return NOW

        return C()

    def _collect(self, policy, store, context=()):
        budget = Budget(float(policy.get("public_budget_seconds", 10)), label="public")
        return collect_public_and_adaptive(
            policy, self._clock(), store=store, context=list(context),
            public_budget=budget,
            planner=FakePlanner(lambda w, p: _plan(query=w["public_topic"], scope="reddit")),
            fetcher=FixtureFetcher({"https://www.reddit.com/search.rss": {"body": RSS}}),
        )

    def _base_policy(self, **over):
        return _policy(feeds_enabled=True, feed_seed_sources=[], discovery_topics_cap=0, **over)

    def test_disabled_yields_no_adaptive_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            _add(store, "quantum networking")
            results = self._collect(self._base_policy(), store)
            packet = build_packet(results, clock=self._clock(), policy=self._base_policy(), baseline={})
            self.assertEqual(packet["candidates"], [])

    def test_enabled_yields_real_candidate_then_change_drops_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            watch = _add(store, "quantum networking")
            policy = self._base_policy(adaptive_mode="active")
            results = self._collect(policy, store)
            packet = build_packet(results, clock=self._clock(), policy=policy, baseline={})
            cands = [c for c in packet["candidates"] if c["kind"] == "feed"]
            self.assertTrue(cands, "enabled adaptive watch must yield a feed candidate")
            old_ref = cands[0]["ref"]
            # build_packet_results is the explicit gate; disabled still returns none.
            self.assertEqual(build_packet_results(results, self._base_policy()), [])
            apply_action(store, {"action": "change", "id": watch["id"],
                                 "public_topic": "renewable energy",
                                 "provenance": "explicit user request"}, NOW)
            results2 = self._collect(policy, store)
            packet2 = build_packet(results2, clock=self._clock(), policy=policy, baseline={})
            refs2 = [c["ref"] for c in packet2["candidates"] if c["kind"] == "feed"]
            self.assertTrue(refs2)
            self.assertNotIn(old_ref, refs2, "changed watch must drop the retired candidate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
