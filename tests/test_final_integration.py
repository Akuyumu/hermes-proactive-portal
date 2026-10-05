"""Final integration controls: shared public HTTP/source budget + native planning.

Every test injects a fake planner and fake fetcher. NO network, no subprocess, no
private/live source, no production model, no /tmp, nothing is sent. Covers the
bounded final corrections:

  1. the ordinary feeds and adaptive evidence draw on ONE shared source-attempt
     allowance, so the combined attempts never exceed ``feed_max_sources_per_run``
     (never 2 ordinary PLUS 2 adaptive);
  2. the adaptive plan/cache refresh runs BEFORE any HTTP request and its
     non-HTTP duration is removed from the shared HTTP budget WITHOUT refilling
     what ordinary feeds already spent;
  3. model planning is DEFERRED honestly when it cannot fit the remaining overall
     collector budget;
  4. a plan with no query is persisted ``unsupported`` rather than a healthy
     ``ok`` with an empty count.

Standalone:
  PYTHONPATH=src python3 -m unittest tests/test_final_integration.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.adaptive import (  # noqa: E402
    PublicAttemptLedger,
    collect_adaptive,
    next_public_rotation,
    prepare_adaptive_plan,
)
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import Budget  # noqa: E402
from proactive_pilot.main import collect_public_and_adaptive  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.publicfeeds import FetchFailed, FetchResponse  # noqa: E402
from proactive_pilot.planner import FakePlanner, PlannerError, plan_for_watch  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.watchstore import (  # noqa: E402
    apply_action,
    load_watchstore,
    save_watchstore,
)

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=SG)

GITHUB_1 = "https://api.github.com/repos/a/b/releases"
GITHUB_2 = "https://api.github.com/repos/c/d/releases"
REDDIT_SEARCH = "https://www.reddit.com/search.rss"

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Mycorrhizal networks advance</title>
    <link>https://example.com/x1</link>
    <description>fungal research</description>
    <pubDate>Sat, 03 Oct 2026 11:00:00 +0800</pubDate>
    <guid>x1</guid></item>
</channel></rss>"""


class RecordingFetcher:
    """Fake public fetcher. Unknown URLs raise so an unapproved host fails loud."""

    def __init__(self, responses=None, log=None):
        self.responses = responses or {}
        self.calls: list[str] = []
        self.log = log if log is not None else []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        self.log.append("fetch:" + url)
        spec = self.responses.get(url)
        if spec is None:
            for key, value in self.responses.items():
                if url.startswith(key):
                    spec = value
                    break
        if spec is None:
            raise FetchFailed(f"no fake response for {url}")
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, FetchResponse):
            return spec
        status = int(spec.get("status", 200))
        body = spec.get("body", "")
        if isinstance(body, str):
            body = body.encode("utf-8")
        return FetchResponse(status, dict(spec.get("headers") or {}), body, url)


def _policy(**over):
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    policy["_preferences"] = {"explicit": [], "inferred": [], "snoozes": []}
    policy.update(over)
    return policy


def _seeds():
    return [
        {"kind": "github_releases", "url": GITHUB_1, "provenance": "t",
         "interest": "r", "cadence_minutes": 60},
        {"kind": "github_releases", "url": GITHUB_2, "provenance": "t",
         "interest": "r", "cadence_minutes": 60},
    ]


def _reddit_plan(query="quantum networking", scope="reddit"):
    return {"schema": 1, "queries": [query], "scope": scope,
            "semantic": True, "provenance": "model"}


def _add(store, topic="quantum networking", scope="reddit", **over):
    action = {"action": "add", "public_topic": topic, "scope": scope,
              "provenance": "explicit user request"}
    action.update(over)
    return apply_action(store, action, NOW)


class CombinedSourceCapTests(unittest.TestCase):
    def test_combined_attempts_never_exceed_feed_max_sources_per_run(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
            _add(store)
            policy = _policy(feeds_enabled=True, feed_seed_sources=_seeds(),
                             discovery_topics_cap=0, adaptive_mode="active")
            fetcher = RecordingFetcher({
                GITHUB_1: {"body": "[]"},
                GITHUB_2: {"body": "[]"},
                REDDIT_SEARCH: {"body": RSS},
            })
            results = collect_public_and_adaptive(
                policy, SeededClock(NOW.isoformat()), store=store, context=[],
                public_budget=Budget(float(policy["public_budget_seconds"])),
                planner=FakePlanner(_reddit_plan()), fetcher=fetcher,
                public_fetcher=fetcher, adaptive_first=True,
            )
            self.assertLessEqual(len(fetcher.calls), 2,
                                 "ordinary + adaptive must share feed_max_sources_per_run")
            # Adaptive got first refusal and is NOT starved out by ordinary feeds.
            self.assertTrue(any(u.startswith(REDDIT_SEARCH) for u in fetcher.calls))
            self.assertIsInstance(results, list)

    def test_ledger_only_consumes_actual_attempts(self):
        # An empty ordinary due set must not silently consume the shared cap.
        ledger = PublicAttemptLedger(2, adaptive_first=False)
        self.assertEqual(ledger.take(0), 0)
        self.assertEqual(ledger.remaining(), 2)
        self.assertEqual(ledger.take(2), 2)
        self.assertEqual(ledger.remaining(), 0)
        self.assertEqual(ledger.take(2), 0)

    def test_fair_rotation_alternates_first_refusal(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
            first = next_public_rotation(store)
            second = next_public_rotation(store)
            third = next_public_rotation(store)
            self.assertFalse(first)
            self.assertTrue(second)
            self.assertFalse(third)


class PlanBeforeHttpTests(unittest.TestCase):
    def test_plan_refresh_precedes_any_http_request(self):
        log: list[str] = []

        class LogPlanner:
            def plan(self, watch, policy):
                log.append("plan")
                return _reddit_plan()

        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
            _add(store)
            policy = _policy(feeds_enabled=True, feed_seed_sources=_seeds()[:1],
                             discovery_topics_cap=0, adaptive_mode="active")
            fetcher = RecordingFetcher({GITHUB_1: {"body": "[]"},
                                        REDDIT_SEARCH: {"body": RSS}}, log=log)
            collect_public_and_adaptive(
                policy, SeededClock(NOW.isoformat()), store=store, context=[],
                public_budget=Budget(float(policy["public_budget_seconds"])),
                planner=LogPlanner(), fetcher=fetcher, public_fetcher=fetcher,
                adaptive_first=False,
            )
            self.assertTrue(log)
            self.assertEqual(log[0], "plan",
                             "planning must run before the first ordinary HTTP request")

    def test_planning_is_excluded_from_the_shared_http_budget(self):
        class SlowPlanner:
            def plan(self, watch, policy):
                time.sleep(0.9)
                return _reddit_plan()

        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
            _add(store)
            policy = _policy(feeds_enabled=True, feed_seed_sources=_seeds()[:1],
                             discovery_topics_cap=0, adaptive_mode="active")
            fetcher = RecordingFetcher({GITHUB_1: {"body": "[]"},
                                        REDDIT_SEARCH: {"body": RSS}})
            # The planner outruns the public HTTP budget: without the exclusion
            # the ordinary feed would see an already-exhausted budget and skip.
            budget = Budget(0.8)
            collect_public_and_adaptive(
                policy, SeededClock(NOW.isoformat()), store=store, context=[],
                public_budget=budget, planner=SlowPlanner(), fetcher=fetcher,
                public_fetcher=fetcher, adaptive_first=False,
            )
            self.assertIn(GITHUB_1, fetcher.calls,
                          "planning time must not consume the shared HTTP budget")

    def test_spent_budget_is_not_refilled_by_adaptive_phase(self):
        # A shared budget already spent by ordinary feeds must not be restarted by
        # the adaptive phase (which would add a second additive cap).
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
            _add(store)
            budget = Budget(10, label="public")
            budget._start -= 6.0  # simulate 6s already spent by ordinary feeds
            self.assertLess(budget.remaining(), 10)
            fetcher = RecordingFetcher({REDDIT_SEARCH: {"body": RSS}})
            collect_adaptive(
                _policy(public_budget_seconds=10), SeededClock(NOW.isoformat()),
                store=store, planner=FakePlanner(_reddit_plan()), fetcher=fetcher,
                budget=budget,
            )
            self.assertLess(budget.remaining(), 10,
                            "adaptive must not refill a budget ordinary feeds spent")


class DeferredPlanningTests(unittest.TestCase):
    def test_model_planning_deferred_when_overall_budget_cannot_fit(self):
        class CountingPlanner:
            def __init__(self):
                self.calls = 0

            def plan(self, watch, policy):
                self.calls += 1
                return _reddit_plan()

        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
            _add(store)
            planner = CountingPlanner()
            prepared = prepare_adaptive_plan(
                _policy(), SeededClock(NOW.isoformat()), store=store, planner=planner,
                overall_budget=Budget(0.2),
            )
            self.assertEqual(prepared["status"], "prepared")
            self.assertTrue(prepared["planning_deferred"])
            self.assertFalse(prepared["used_model"])
            self.assertEqual(planner.calls, 0, "deferred planning must not call the model")
            self.assertEqual(prepared["plan"]["provenance"], "deterministic-nonsemantic")


class EmptyQueryUnsupportedTests(unittest.TestCase):
    def test_cached_empty_query_plan_is_unsupported_not_ok_empty(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
            watch = _add(store)
            data = load_watchstore(store, strict=True)
            data["watches"][watch["id"]]["plan"] = {
                "schema": 1, "watch_id": watch["id"], "watch_version": 1,
                "queries": [], "scope": "reddit", "semantic": True,
                "provenance": "model", "planned_at": NOW.isoformat(),
            }
            data["watches"][watch["id"]]["plan_version"] = 1
            data["watches"][watch["id"]]["plan_at"] = NOW.isoformat()
            save_watchstore(store, data)

            fetcher = RecordingFetcher({REDDIT_SEARCH: {"body": RSS}})
            summary = collect_adaptive(
                _policy(), SeededClock(NOW.isoformat()), store=store,
                planner=FakePlanner(_reddit_plan()), fetcher=fetcher,
            )
            self.assertEqual(summary["status"], "unsupported")
            self.assertEqual(summary["sources"][0]["status"], "unsupported")
            self.assertEqual(fetcher.calls, [], "an empty plan must not fetch")


class DefaultNativePlannerTests(unittest.TestCase):
    def test_plan_for_watch_honours_explicit_plan_timeout(self):
        # The separate planner bound is respected and a tool-exposing planner
        # still falls back honestly rather than fabricating a model result.
        policy = _policy()
        watch = {"id": "w1", "version": 1, "scope": "reddit",
                 "public_topic": "quantum networking", "include": [], "exclude": []}

        class Raising:
            def plan(self, w, p):
                raise PlannerError("no model")

        plan, used = plan_for_watch(watch, policy, Raising(), now=NOW, plan_timeout=0.2)
        self.assertFalse(used)
        self.assertEqual(plan["provenance"], "deterministic-nonsemantic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
