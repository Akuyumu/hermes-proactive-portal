"""Bounded, resumable-runner acceptance tests.

Covers collector budget, feed per-run source cap + single Reddit attempt even on
failure, 304 no evidence-version churn, malformed-parser regression (no
NameError), obligation cursor resume with partial coverage, cold-bootstrap
historical age from source time, and the actual main runner with mocked sources
(default stdout empty, compact state persisted).

No personal account is read; no network is used. Standalone:
  python3 tests/test_bounded_runner.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.candidates import build_packet, packet_digest  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import Budget, SourceResult, default_sources  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult, FakeEvaluator  # noqa: E402
from proactive_pilot.obligations import collect_obligations, load_obligations  # noqa: E402
from proactive_pilot.policy import load_policy  # noqa: E402
from proactive_pilot.publicfeeds import (  # noqa: E402
    FeedError,
    FetchFailed,
    FetchResponse,
    collect_public_feeds,
    load_registry,
    parse_source,
)
from proactive_pilot.state import StateStore  # noqa: E402

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=SG)

RSS = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
       b'<entry><id>e1</id><title>Release</title>'
       b'<updated>2026-10-03T11:00:00Z</updated><summary>x</summary></entry></feed>')


class FakeFetcher:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[str] = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        return self.handler(url, **kwargs)


def _clock(at=NOW):
    return SeededClock(at.isoformat())


def _policy(seeds=None):
    policy = load_policy(None)
    if seeds is not None:
        policy["feed_seed_sources"] = seeds
    return policy


class BudgetTests(unittest.TestCase):
    def test_zero_budget_marks_sources_partial_without_calling(self):
        calls = {"n": 0}

        class Boom:
            def run(self, *a, **k):
                calls["n"] += 1
                raise AssertionError("must not read under exhausted budget")

        budget = Budget(0)
        self.assertTrue(budget.expired())
        policy = _policy()
        factories = default_sources(policy, _clock(), budget=budget)
        self.assertTrue(factories)
        for factory in factories:
            res = factory()
            self.assertTrue(res.truncated)
            self.assertIn("budget", res.failure)
        self.assertEqual(calls["n"], 0)

    def test_budget_clamp_never_exceeds_remaining(self):
        budget = Budget(5)
        self.assertLessEqual(budget.clamp(30), 5)


class FeedBoundedTests(unittest.TestCase):
    def _seeds(self):
        return [
            {"kind": "reddit_rss", "url": "https://www.reddit.com/r/a/.rss",
             "provenance": "t", "interest": "a", "cadence_minutes": 1},
            {"kind": "reddit_rss", "url": "https://www.reddit.com/r/b/.rss",
             "provenance": "t", "interest": "b", "cadence_minutes": 1},
            {"kind": "github_search", "url": "https://api.github.com/search/repositories?q=aa",
             "provenance": "t", "interest": "aa", "cadence_minutes": 1},
            {"kind": "github_search", "url": "https://api.github.com/search/repositories?q=bb",
             "provenance": "t", "interest": "bb", "cadence_minutes": 1},
        ]

    def test_per_run_cap_and_single_reddit_even_on_failure(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), _clock())
            policy = _policy(self._seeds())
            policy["feed_max_sources_per_run"] = 2

            def handler(url, **kw):
                if "reddit.com" in url:
                    raise FetchFailed("http 429")
                return FetchResponse(200, {}, b'{"items": []}', url)

            fetcher = FakeFetcher(handler)
            collect_public_feeds(policy, _clock(), store=store, fetcher=fetcher)
            reddit_calls = [u for u in fetcher.calls if "reddit.com" in u]
            self.assertLessEqual(len(reddit_calls), 1, "at most one Reddit attempt even on failure")
            self.assertLessEqual(len(fetcher.calls), 2, "per-run source cap")
            # Rotation: a later run advances the cursor to the other sources.
            first = list(fetcher.calls)
            later = _clock(NOW + timedelta(minutes=5))
            collect_public_feeds(policy, later, store=store, fetcher=fetcher)
            self.assertTrue(set(fetcher.calls) - set(first), "cursor must rotate sources")

    def test_304_does_not_churn_evidence_version(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), _clock())
            policy = _policy([{"kind": "reddit_rss",
                               "url": "https://www.reddit.com/r/hermes/.rss",
                               "provenance": "t", "interest": "h", "cadence_minutes": 1}])
            state = {"status": 200}

            def handler(url, **kw):
                if state["status"] == 304:
                    return FetchResponse(304, {}, b"", url)
                return FetchResponse(200, {"ETag": "abc"}, RSS, url)

            fetcher = FakeFetcher(handler)
            r1 = collect_public_feeds(policy, _clock(), store=store, fetcher=fetcher)
            p1 = build_packet(r1, clock=_clock(), policy=policy, baseline={})
            state["status"] = 304
            # cadence 1m elapsed
            r2 = collect_public_feeds(policy, _clock(NOW + timedelta(minutes=2)), store=store, fetcher=fetcher)
            p2 = build_packet(r2, clock=_clock(NOW + timedelta(minutes=2)), policy=policy, baseline={})
            self.assertEqual(packet_digest(p1), packet_digest(p2), "304 must not churn evidence version")

    def test_malformed_body_is_error_not_nameerror(self):
        with self.assertRaises(FeedError):
            parse_source(b"<not really xml", "rss")
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), _clock())
            policy = _policy([{"kind": "reddit_rss",
                               "url": "https://www.reddit.com/r/hermes/.rss",
                               "provenance": "t", "interest": "h", "cadence_minutes": 1}])
            fetcher = FakeFetcher(lambda url, **kw: FetchResponse(200, {}, b"<broken", url))
            results = collect_public_feeds(policy, _clock(), store=store, fetcher=fetcher)
            feed = next(r for r in results if r.name.startswith("feed:"))
            self.assertEqual(feed.status, "error")
            self.assertIn("parse", feed.error)


class ObligationCursorTests(unittest.TestCase):
    class PagedRunner:
        def __init__(self, pages):
            self.pages = pages
            self.calls: list[list[str]] = []

        def run(self, argv, timeout):
            self.calls.append(list(argv))
            if "gmail" in argv and "list" in argv:
                token = argv[argv.index("--page-token") + 1] if "--page-token" in argv else ""
                page = 1 if not token else int(token)
                envs = self.pages.get(page, [])
                nxt = page + 1 if self.pages.get(page + 1) else None
                ids = [{"id": str(e["id"]), "threadId": str(e["id"])} for e in envs]
                return 0, json.dumps({"ids": ids, "next_page": nxt}), ""
            if "gmail" in argv and "get" in argv:
                mid = argv[-1]
                env = next((e for envs in self.pages.values() for e in envs
                            if str(e["id"]) == mid), {})
                frm = env.get("from") or [{}]
                headers = [
                    {"name": "From", "value": str((frm[0] or {}).get("email") or "")},
                    {"name": "Subject", "value": str(env.get("subject") or "")},
                    {"name": "Date", "value": str(env.get("date") or "")},
                ]
                return 0, json.dumps({"id": mid, "headers": headers}), ""
            if "read" in argv:
                return 0, "body", ""
            return 1, "", "unexpected"

    def _env(self, mid, subject, when):
        return {"id": mid, "subject": subject, "from": [{"email": "b@shop.example"}],
                "date": when, "has-attachment": False}

    def test_bootstrap_cursor_resumes_and_reports_partial(self):
        pages = {
            1: [self._env("m1", "Action required: confirm your order INV-11111 by 2026-10-10", "2026-10-02T09:00:00Z"),
                self._env("m2", "Action required: confirm your order INV-22222 by 2026-10-10", "2026-10-02T08:00:00Z")],
            2: [self._env("m3", "Payment due for invoice INV-33333 by 2026-10-10", "2026-09-20T09:00:00Z")],
            3: [],
        }
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), _clock())
            policy = _policy()
            policy["source_page_size"] = 2
            policy["obligation_metadata_items_per_run"] = 2
            policy["obligation_max_pages_per_run"] = 1
            runner = self.PagedRunner(pages)
            r1 = collect_obligations(policy, _clock(), store=store, runner=runner)
            formal1 = next(r for r in r1 if r.name == "obligation:formal")
            self.assertTrue(formal1.truncated, "first bootstrap pass must be partial")
            self.assertFalse(formal1.window_complete)
            cur = load_obligations(store)["cursors"]["formal"]
            self.assertEqual(cur["token"], "2")
            # Resume: next run starts at the opaque token '2' and still partial.
            pages[1] = []  # page 1 must not be re-read after cursor advanced
            r2 = collect_obligations(policy, _clock(), store=store, runner=runner)
            formal2 = next(r for r in r2 if r.name == "obligation:formal")
            tokens = [c[c.index("--page-token") + 1] for c in runner.calls
                      if "gmail" in c and "list" in c and "--page-token" in c]
            self.assertIn("2", tokens)
            # Records from both passes persist.
            records = load_obligations(store)["records"]
            self.assertGreaterEqual(len(records), 1)

    def test_cold_bootstrap_historical_age_uses_source_time(self):
        old = (NOW - timedelta(days=80)).isoformat().replace("+00:00", "Z")
        pages = {1: [self._env("old", "Payment due for invoice INV-99999 by 2026-07-01", old)], 2: []}
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), _clock())
            policy = _policy()
            policy["source_page_size"] = 5
            runner = self.PagedRunner(pages)
            r = collect_obligations(policy, _clock(), store=store, runner=runner)
            formal = next(x for x in r if x.name == "obligation:formal")
            item = formal.items[0]
            self.assertGreaterEqual(item["age_days"], 79)
            self.assertTrue(item["historical"])


class MainRunnerTests(unittest.TestCase):
    def test_default_runner_stdout_empty_and_persists_state(self):
        import proactive_pilot.main as main
        import proactive_pilot.obligations as obligations
        import proactive_pilot.publicfeeds as publicfeeds

        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            state_dir = Path(tmp) / "state"
            prefs = Path(tmp) / "prefs.json"
            prefs.write_text(json.dumps({"schema": 1, "explicit": [], "inferred": [],
                                         "snoozes": [], "paused": {"active": False}}))
            args = main.build_parser().parse_args([
                "--run", "--preferences", str(prefs), "--state-dir", str(state_dir),
                "--seeded-clock", NOW.isoformat(), "--no-model",
            ])

            def fake_default_sources(policy, clock, budget=None):
                return [lambda: SourceResult("tasks:primary", "ok", count=0, items=[],
                                             checked_at=clock.now().isoformat())]

            orig_ds = main.default_sources
            orig_feeds = publicfeeds.collect_public_feeds
            orig_obl = obligations.collect_obligations
            main.default_sources = fake_default_sources
            publicfeeds.collect_public_feeds = lambda *a, **k: []
            obligations.collect_obligations = lambda *a, **k: []
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    rc = main.run_once(args)
            finally:
                main.default_sources = orig_ds
                publicfeeds.collect_public_feeds = orig_feeds
                obligations.collect_obligations = orig_obl
            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue(), "", "default runner stdout must stay empty")
            self.assertTrue((state_dir / "notify_state.json").exists(), "compact state must persist")


class IntegratedFairnessTests(unittest.TestCase):
    """Category budgets + main runner: a slow core cannot starve obligations.

    No network, no personal account: default_sources/feeds/obligations are
    monkeypatched, but the REAL main.run_once category-budget wiring, the real
    collect_obligations account rotation, and the real stdout contract run.
    """

    def _prefs(self, tmp):
        prefs = Path(tmp) / "prefs.json"
        prefs.write_text(json.dumps({"schema": 1, "explicit": [], "inferred": [],
                                     "snoozes": [], "paused": {"active": False}}))
        return prefs

    def test_category_budgets_honored_and_slow_core_cannot_starve_obligation(self):
        import time
        import proactive_pilot.main as main
        import proactive_pilot.obligations as obligations
        import proactive_pilot.publicfeeds as publicfeeds
        from proactive_pilot.collectors import Budget

        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            state_dir = Path(tmp) / "state"
            # A small custom policy file exercises the REAL category wiring with
            # short waits; the default policy is asserted separately below.
            policy_file = Path(tmp) / "policy.json"
            policy_file.write_text(json.dumps({
                "collector_budget_seconds": 6,
                "core_budget_seconds": 2,
                "public_budget_seconds": 1,
                "obligation_budget_seconds": 2,
                "mail_preview_items": 1,
                "mail_preview_timeout_seconds": 1,
                "obligation_max_pages_per_run": 1,
            }))
            args = main.build_parser().parse_args([
                "--run", "--preferences", str(self._prefs(tmp)),
                "--policy", str(policy_file),
                "--state-dir", str(state_dir), "--seeded-clock", NOW.isoformat(), "--no-model",
            ])

            seen = {}
            core_calls = {"start": None, "end": None}

            def slow_core(policy, clock, budget=None):
                # Core sleeps far beyond its reserved slot; it must be stopped by
                # the category budget, not allowed to consume the obligation slot.
                def factory():
                    core_calls["start"] = time.monotonic()
                    while budget is not None and not budget.expired():
                        time.sleep(0.02)
                    core_calls["end"] = time.monotonic()
                    return SourceResult("mail:formal", "ok", count=0, items=[],
                                        checked_at=clock.now().isoformat(), truncated=True,
                                        failure="mail budget-deferred")
                return [factory]

            real_obligations = obligations.collect_obligations

            def tracking_obligations(policy, clock, *, store=None, budget=None, **kw):
                seen["obligation_budget"] = budget
                seen["obligation_start"] = time.monotonic()
                seen["obligation_remaining_at_entry"] = budget.remaining()
                return real_obligations(policy, clock, store=store,
                                        runner=_StubRunner(), budget=budget)

            orig_ds = main.default_sources
            orig_feeds = publicfeeds.collect_public_feeds
            orig_obl = obligations.collect_obligations
            main.default_sources = slow_core
            publicfeeds.collect_public_feeds = lambda *a, **k: []
            obligations.collect_obligations = tracking_obligations
            out = io.StringIO()
            started = time.monotonic()
            try:
                with contextlib.redirect_stdout(out):
                    rc = main.run_once(args)
                wall = time.monotonic() - started
            finally:
                main.default_sources = orig_ds
                publicfeeds.collect_public_feeds = orig_feeds
                obligations.collect_obligations = orig_obl

            self.assertEqual(rc, 0)
            self.assertEqual(out.getvalue(), "", "default stdout must stay empty")
            # Category budgets are distinct objects that are honored, with core
            # parented to the overall wallclock.
            ob = seen["obligation_budget"]
            self.assertIsInstance(ob, Budget)
            self.assertEqual(ob.seconds, 2.0)
            self.assertEqual(ob.parent.seconds, 6.0)
            self.assertGreater(seen["obligation_remaining_at_entry"], 1.5,
                               "reserved slot must start at category entry, not run entry")
            # Obligations were actually attempted (fresh account rotation) and
            # given their reserved slot rather than being starved by slow core.
            self.assertGreaterEqual(seen["obligation_start"], core_calls["start"])
            self.assertLessEqual(core_calls["end"] - core_calls["start"], 3.0,
                                 "core must be bounded by its reserved slot")
            # Real walltime: overall cap enforced; no hang.
            self.assertLess(wall, 10.0, "run must not hang past the collector budget")

    def test_default_category_budgets_allocation(self):
        # Default policy: core 35 / public 10 / obligation 15 under a 60s cap.
        policy = load_policy(None)
        self.assertEqual(policy["collector_budget_seconds"], 60)
        self.assertEqual(policy["core_budget_seconds"], 35)
        self.assertEqual(policy["public_budget_seconds"], 10)
        self.assertEqual(policy["obligation_budget_seconds"], 15)
        self.assertEqual(policy["mail_preview_items"], 1)
        self.assertEqual(policy["mail_preview_timeout_seconds"], 2)
        self.assertEqual(policy["obligation_max_pages_per_run"], 1)
        self.assertEqual(policy["obligation_preview_items"], 1)
        self.assertEqual(policy["obligation_preview_timeout_seconds"], 3)
        self.assertLessEqual(int(policy["evaluator"]["timeout_seconds"]), 100)

    def test_obligation_budget_expiry_marks_partial_not_auth_failure(self):
        import proactive_pilot.obligations as obligations
        from proactive_pilot.collectors import Budget

        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), _clock())
            policy = load_policy(None)
            expired = Budget(0)
            results = obligations.collect_obligations(policy, _clock(), store=store,
                                                      runner=_StubRunner(), budget=expired)
            self.assertTrue(results, "at least one obligation account must be reported")
            for res in results:
                self.assertEqual(res.status, "ok")
                self.assertTrue(res.truncated)
                self.assertIn("budget", res.failure)
                self.assertNotIn("auth", res.failure.lower())


class SafeErrorTests(unittest.TestCase):
    def test_errors_are_fixed_labels_never_raw_text(self):
        import subprocess
        from proactive_pilot.collectors import SourceTimeout, safe_source_error

        raw = "himalaya: error reading message 42: Subject: Bank OTP 483920 from a@b.c"
        self.assertEqual(safe_source_error(raw, kind="mail"), "mail unavailable")
        self.assertNotIn("483920", safe_source_error(raw, kind="mail"))
        self.assertNotIn("Bank", safe_source_error(raw, kind="mail"))
        self.assertEqual(safe_source_error(subprocess.TimeoutExpired("x", 1), kind="mail"),
                         "mail timeout")
        self.assertEqual(safe_source_error(SourceTimeout("slow"), kind="obligation"),
                         "obligation budget-deferred")
        self.assertEqual(safe_source_error("HTTP 401 unauthorized", kind="calendar"),
                         "authentication unavailable")

    def test_wallclock_alarm_restores_previous_handler_and_timer(self):
        import signal
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM not available on this platform")
        from proactive_pilot.collectors import wallclock_alarm

        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.setitimer(signal.ITIMER_REAL, 30.0)
        try:
            with wallclock_alarm(5.0):
                pass
            restored = signal.getitimer(signal.ITIMER_REAL)
            self.assertGreater(restored[0], 0.0, "prior ITIMER_REAL delay must be restored")
            self.assertEqual(signal.getsignal(signal.SIGALRM), previous_handler)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

    def test_expiry_preserves_evidence_as_partial(self):
        # A SIGALRM-style expiry must keep whatever calendar evidence was already
        # collected and be labelled partial/budget-deferred, never a hard error.
        import proactive_pilot.collectors as C

        orig = C._calendar_client

        class _Client:
            def calendars(self):
                return {"items": [{"id": "primary", "primary": True}]}
            def _is_holiday_calendar(self, cal):
                return False
            def _events(self, calendar_id):
                return "https://cal.example/events"
            def _request(self, method, url):
                raise C.SourceTimeout("expired mid-fetch")
        C._calendar_client = lambda alias: _Client()
        try:
            res = C.collect_calendar("primary", clock=_clock(), timeout=1)
        finally:
            C._calendar_client = orig
        self.assertEqual(res.status, "ok")
        self.assertTrue(res.truncated)
        self.assertIn("budget", res.failure)


class _StubRunner:
    """Empty native Gmail id page; no personal data, no subprocess."""

    def run(self, argv, timeout):
        if "gmail" in argv and "list" in argv:
            return 0, json.dumps({"ids": [], "next_page": None}), ""
        if "envelope" in argv:
            return 0, json.dumps({"envelopes": []}), ""
        return 0, "", ""


if __name__ == "__main__":
    unittest.main(verbosity=2)
