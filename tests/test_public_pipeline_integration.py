"""Offline public-source integration through the ordinary main pipeline.

The saved NEA/SMRT fixtures are local. Every client is injected; no socket,
private source, evaluator runtime, or native send is used.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _sample(name: str) -> bytes:
    """Read a reviewed public sample from the canonical ``public_samples`` directory."""
    path = ROOT / "public_samples" / name
    if path.is_file():
        return path.read_bytes()
    raise FileNotFoundError(f"sample fixture not found: {name}")


from proactive_pilot import main as M  # noqa: E402
from proactive_pilot.candidates import build_packet  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult, FakeEvaluator  # noqa: E402
from proactive_pilot.publicfeeds import (  # noqa: E402
    FeedAPIError,
    FeedExpired,
    FeedFailed,
    FetchResponse,
    NEA_FORECAST_URL,
    SMRT_F1_NOTICE_URL,
    parse_nea_forecast,
    parse_smrt_planned_notice,
    collect_public_feeds,
)
from proactive_pilot.state import StateStore  # noqa: E402

SG = "+08:00"
NOW = datetime.fromisoformat("2026-10-04T18:30:00" + SG)
NEA_BODY = _sample("nea_forecast.json")
SMRT_BODY = _sample("smrt_f1_notice.html")


class _FakeFetcher:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.overrides: dict[str, FetchResponse] = {}

    def __call__(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if url in self.overrides:
            return self.overrides[url]
        if url == NEA_FORECAST_URL:
            return FetchResponse(200, {"Content-Type": "application/json"}, NEA_BODY, url)
        if url == SMRT_F1_NOTICE_URL:
            return FetchResponse(200, {"Content-Type": "text/html"}, SMRT_BODY, url)
        raise AssertionError(f"unexpected URL {url}")


class PublicParserTests(unittest.TestCase):
    def test_nea_fixture_preserves_update_and_validity_as_forecast(self):
        row = parse_nea_forecast(NEA_BODY, now=NOW)[0]
        self.assertEqual(row["category"], "weather_forecast")
        self.assertTrue(row["forecast_only"])
        self.assertEqual(row["publisher"], "Singapore National Environment Agency (NEA)")
        self.assertEqual(row["valid_from"], "2026-10-04T18:00:00+08:00")
        self.assertEqual(row["valid_until"], "2026-10-05T18:00:00+08:00")
        self.assertIn("2026-10-04T17:40:59+08:00", row["summary"])

    def test_nea_rejects_expired_stale_malformed_and_api_error(self):
        with self.assertRaises(FeedExpired):
            parse_nea_forecast(NEA_BODY, now=datetime.fromisoformat("2026-10-05T18:01:00+08:00"))
        with self.assertRaises(FeedExpired):
            parse_nea_forecast(NEA_BODY, now=datetime.fromisoformat("2026-10-05T07:00:00+08:00"))
        with self.assertRaises(FeedAPIError):
            parse_nea_forecast(NEA_BODY.replace(b'"code":0', b'"code":429'), now=NOW)
        with self.assertRaises(FeedFailed):
            parse_nea_forecast(b'{"code":0,"data":{"records":[]}}', now=NOW)

    def test_smrt_fixture_keeps_publication_validity_operator_and_lines(self):
        row = parse_smrt_planned_notice(SMRT_BODY, now=NOW)[0]
        self.assertEqual(row["category"], "planned_transport")
        self.assertEqual(row["publisher"], "SMRT")
        self.assertEqual(row["published"], "2026-09-04")
        self.assertEqual(row["valid_from"], "2026-10-09")
        self.assertEqual(row["valid_until"], "2026-10-11")
        self.assertEqual(row["affected_lines"], [
            "North-South Line", "East-West Line", "Circle Line", "Thomson-East Coast Line"
        ])
        self.assertIn("not a live incident or all-clear", row["summary"])

    def test_smrt_expired_missing_and_ambiguous_dates_fail_closed(self):
        expired = SMRT_BODY.replace(b"from 9 to 11 October 2026", b"from 9 to 11 October 2025")
        with self.assertRaises(FeedExpired):
            parse_smrt_planned_notice(expired, now=NOW)
        with self.assertRaises(FeedFailed):
            parse_smrt_planned_notice(b"<h3>SMRT Travel Advisory for Formula One 2026</h3>", now=NOW)
        ambiguous = SMRT_BODY.replace(
            b"from 9 to 11 October 2026",
            b"from 9 to 11 October 2026 and from 10 to 12 October 2026",
        )
        with self.assertRaises(FeedFailed):
            parse_smrt_planned_notice(ambiguous, now=NOW)


class OrdinaryMainPipelineTests(unittest.TestCase):
    def test_fake_clients_flow_through_main_collector_packet_and_evaluator(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            root = Path(temp)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps({
                "timezone": "Asia/Singapore",
                "location_context_enabled": False,
                "feeds_enabled": True,
                "obligations_enabled": False,
                "feed_max_sources_per_run": 2,
                "public_budget_seconds": 10,
                "feed_timeout_seconds": 5,
                "feed_max_bytes": 200_000,
                "feed_max_items": 5,
                "feed_cadence_minutes": 360,
                "feed_seed_sources": [
                    {"kind": "nea_forecast", "url": NEA_FORECAST_URL,
                     "provenance": "fixture source", "interest": "weather", "cadence_minutes": 360},
                    {"kind": "smrt_notice", "url": SMRT_F1_NOTICE_URL,
                     "provenance": "fixture source", "interest": "planned transport", "cadence_minutes": 360},
                ],
            }), encoding="utf-8")
            preferences = root / "preferences.json"
            preferences.write_text(json.dumps({
                "schema": 1, "version": 0, "explicit": [], "inferred": [],
                "snoozes": [], "paused": {"active": False},
            }), encoding="utf-8")
            fetcher = _FakeFetcher()
            evaluator = FakeEvaluator(EvaluatorResult("silence", reason="synthetic evaluator"))
            args = M.build_parser().parse_args([
                "--dry-run", "--seeded-clock", "2026-10-04T18:30:00+08:00",
                "--policy", str(policy_path), "--preferences", str(preferences),
                "--state-dir", str(root / "state"),
            ])
            args.public_fetcher = fetcher
            old_sources = M.default_sources
            M.default_sources = lambda *a, **k: [
                lambda: SourceResult("calendar:synthetic", "ok", checked_at=NOW.isoformat())
            ]
            try:
                code = M._run_once(args, evaluator=evaluator)
            finally:
                M.default_sources = old_sources

        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in fetcher.calls], [NEA_FORECAST_URL, SMRT_F1_NOTICE_URL])
        self.assertTrue(all(call[1]["max_bytes"] == 200_000 for call in fetcher.calls))
        self.assertEqual(len(evaluator.calls), 1)
        packet = evaluator.calls[0]
        special = {candidate["category"]: candidate for candidate in packet["candidates"]
                   if candidate.get("category")}
        self.assertIn("weather_forecast", special)
        self.assertIn("planned_transport", special)
        self.assertTrue(special["weather_forecast"]["forecast_only"])
        self.assertTrue(special["planned_transport"]["planned_notice"])
        self.assertEqual(special["planned_transport"]["publisher"], "SMRT")
        self.assertEqual(special["planned_transport"]["valid_until"], "2026-10-11")

    def test_expired_and_rate_limited_sources_stay_stale_during_cadence(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            store = StateStore(Path(temp), SeededClock(NOW.isoformat()))
            fetcher = _FakeFetcher()
            policy = {
                "feeds_enabled": True,
                "feed_max_sources_per_run": 1,
                "feed_max_items": 5,
                "feed_timeout_seconds": 5,
                "feed_max_bytes": 200_000,
                "feed_seed_sources": [{
                    "kind": "nea_forecast", "url": NEA_FORECAST_URL,
                    "provenance": "fixture source", "interest": "weather",
                    "cadence_minutes": 360,
                }],
            }
            first_clock = SeededClock("2026-10-04T18:30:00+08:00")
            fresh = collect_public_feeds(policy, first_clock, store=store, fetcher=fetcher)
            stale_clock = SeededClock("2026-10-05T07:00:00+08:00")
            expired = collect_public_feeds(policy, stale_clock, store=store, fetcher=fetcher)
            cadence_clock = SeededClock("2026-10-05T08:00:00+08:00")
            carried = collect_public_feeds(policy, cadence_clock, store=store, fetcher=fetcher)

            self.assertEqual(fresh[0].status, "ok")
            self.assertEqual(expired[0].status, "stale")
            self.assertEqual(carried[0].status, "stale")
            self.assertEqual(carried[0].items, [], "expired cached forecast must not reappear as a candidate")

            fetcher.overrides[NEA_FORECAST_URL] = FetchResponse(
                429, {}, b"", NEA_FORECAST_URL
            )
            limited_clock = SeededClock("2026-10-05T13:01:00+08:00")
            limited = collect_public_feeds(policy, limited_clock, store=store, fetcher=fetcher)
            next_clock = SeededClock("2026-10-05T14:00:00+08:00")
            limited_carry = collect_public_feeds(policy, next_clock, store=store, fetcher=fetcher)
            self.assertEqual(limited[0].status, "stale")
            self.assertEqual(limited_carry[0].status, "stale")
            self.assertEqual(limited_carry[0].items, [])


def _single_seed_policy(seed, *, per_run=1):
    return {
        "feeds_enabled": True,
        "feed_max_sources_per_run": per_run,
        "feed_max_items": 5,
        "feed_timeout_seconds": 5,
        "feed_max_bytes": 200_000,
        "feed_seed_sources": [seed],
    }


NEA_SEED = {
    "kind": "nea_forecast", "url": NEA_FORECAST_URL,
    "provenance": "fixture source", "interest": "weather",
    "cadence_minutes": 2880,
}
SMRT_SEED = {
    "kind": "smrt_notice", "url": SMRT_F1_NOTICE_URL,
    "provenance": "fixture source", "interest": "planned transport",
    "cadence_minutes": 20160,
}


class CachedExpiryTests(unittest.TestCase):
    """Conservative structured-cache validity when carried without a fetch."""

    def test_cached_forecast_expiry_before_cadence_is_not_a_candidate(self):
        # Ordinary main -> collector -> packet -> evaluator flow.
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            root = Path(temp)
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps({
                "timezone": "Asia/Singapore",
                "location_context_enabled": False,
                "feeds_enabled": True,
                "obligations_enabled": False,
                "feed_max_sources_per_run": 1,
                "public_budget_seconds": 10,
                "feed_timeout_seconds": 5,
                "feed_max_bytes": 200_000,
                "feed_max_items": 5,
                "feed_cadence_minutes": 2880,
                "feed_seed_sources": [dict(NEA_SEED)],
            }), encoding="utf-8")
            preferences = root / "preferences.json"
            preferences.write_text(json.dumps({
                "schema": 1, "version": 0, "explicit": [], "inferred": [],
                "snoozes": [], "paused": {"active": False},
            }), encoding="utf-8")
            fetcher = _FakeFetcher()
            evaluator = FakeEvaluator(EvaluatorResult("silence", reason="synthetic evaluator"))

            def run(clock_iso):
                args = M.build_parser().parse_args([
                    "--dry-run", "--seeded-clock", clock_iso,
                    "--policy", str(policy_path), "--preferences", str(preferences),
                    "--state-dir", str(root / "state"),
                ])
                args.public_fetcher = fetcher
                old_sources = M.default_sources
                M.default_sources = lambda *a, **k: [
                    lambda: SourceResult("calendar:synthetic", "ok", checked_at=clock_iso)
                ]
                try:
                    return M._run_once(args, evaluator=evaluator)
                finally:
                    M.default_sources = old_sources

            self.assertEqual(run(NOW.isoformat()), 0)
            self.assertEqual([c[0] for c in fetcher.calls], [NEA_FORECAST_URL])
            first = {c.get("category") for c in evaluator.calls[-1]["candidates"]}
            self.assertIn("weather_forecast", first)

            # valid_until is 2026-10-05T18:00. Advance one minute past it while
            # still well inside the 2880-minute cadence: carry is a local check.
            self.assertEqual(run("2026-10-05T18:01:00+08:00"), 0)
            self.assertEqual([c[0] for c in fetcher.calls], [NEA_FORECAST_URL],
                             "expired carried cache must not be refetched")
            second_packet = evaluator.calls[-1]
            second = {c.get("category") for c in second_packet["candidates"]}
            self.assertNotIn("weather_forecast", second)
            self.assertTrue(any(v.get("status") == "stale"
                                for v in second_packet["coverage"].values()))

    def test_cached_forecast_exact_validity_boundary_is_stale(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            store = StateStore(Path(temp), SeededClock(NOW.isoformat()))
            fetcher = _FakeFetcher()
            policy = _single_seed_policy(dict(NEA_SEED))
            fresh = collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
            self.assertEqual(fresh[0].status, "ok")
            boundary = collect_public_feeds(
                policy, SeededClock("2026-10-05T18:00:00+08:00"), store=store, fetcher=fetcher)
            self.assertEqual(boundary[0].status, "stale")
            self.assertEqual(boundary[0].items, [])
            self.assertEqual(len(fetcher.calls), 1, "boundary carry must not refetch")

    def test_cached_forecast_malformed_validity_is_unavailable(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            store = StateStore(Path(temp), SeededClock(NOW.isoformat()))
            fetcher = _FakeFetcher()
            policy = _single_seed_policy(dict(NEA_SEED))
            collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
            registry = store.load("public_registry.json", {})
            source = next(iter(registry["sources"].values()))
            for item in source["cache_items"]:
                item.pop("valid_until", None)
            store.save("public_registry.json", registry)
            carried = collect_public_feeds(
                policy, SeededClock("2026-10-04T19:30:00+08:00"), store=store, fetcher=fetcher)
            self.assertEqual(carried[0].status, "stale")
            self.assertEqual(carried[0].items, [])
            self.assertIn("validity", carried[0].failure)
            packet = build_packet(carried, clock=SeededClock("2026-10-04T19:30:00+08:00"),
                                  policy=policy, baseline={})
            self.assertEqual(packet["candidates"], [])

    def test_cached_planned_notice_expiry_is_not_a_candidate(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            store = StateStore(Path(temp), SeededClock(NOW.isoformat()))
            fetcher = _FakeFetcher()
            policy = _single_seed_policy(dict(SMRT_SEED))
            fresh = collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
            self.assertEqual(fresh[0].status, "ok")
            self.assertEqual(fresh[0].items[0]["category"], "planned_transport")
            after = collect_public_feeds(
                policy, SeededClock("2026-10-12T12:00:00+08:00"), store=store, fetcher=fetcher)
            self.assertEqual(after[0].status, "stale")
            self.assertEqual(after[0].items, [])
            self.assertEqual(len(fetcher.calls), 1, "expired notice carry must not refetch")
            packet = build_packet(after, clock=SeededClock("2026-10-12T12:00:00+08:00"),
                                  policy=policy, baseline={})
            self.assertEqual(packet["candidates"], [])

    def test_source_cap_deferred_expired_structured_cache_stays_stale(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            store = StateStore(Path(temp), SeededClock(NOW.isoformat()))
            fetcher = _FakeFetcher()
            seed_policy = _single_seed_policy(dict(NEA_SEED), per_run=2)
            seed_policy["feed_seed_sources"].append(dict(SMRT_SEED))
            first = collect_public_feeds(seed_policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
            self.assertTrue(all(r.status == "ok" for r in first))
            registry = store.load("public_registry.json", {})
            smrt = next(rec for rec in registry["sources"].values()
                        if rec.get("kind") == "smrt_notice")
            smrt_name = f"feed:{smrt['id']}"

            # Both sources are now due (cadence 1m); the per-run cap attempts only
            # the first, deferring SMRT with an already-elapsed validity window.
            seed_policy["feed_seed_sources"][0]["cadence_minutes"] = 1
            seed_policy["feed_seed_sources"][1]["cadence_minutes"] = 1
            seed_policy["feed_max_sources_per_run"] = 1
            second = collect_public_feeds(
                seed_policy, SeededClock("2026-10-12T12:00:00+08:00"), store=store, fetcher=fetcher)
            deferred = next(r for r in second if r.name == smrt_name)
            self.assertEqual(deferred.status, "stale")
            self.assertEqual(deferred.items, [])
            self.assertEqual(deferred.failure, "structured cache expired")
            self.assertEqual(
                [call[0] for call in fetcher.calls].count(SMRT_F1_NOTICE_URL), 1,
                "the deferred SMRT source must not be refetched")

    def test_healthy_generic_cache_carry_is_unaffected(self):
        url = "https://api.github.com/repos/NousResearch/hermes-agent/releases"
        body = json.dumps([{
            "id": 1, "name": "v1.0", "body": "release notes",
            "html_url": "https://github.com/NousResearch/hermes-agent/releases/tag/v1.0",
            "published_at": "2026-10-01T00:00:00Z",
        }]).encode("utf-8")
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as temp:
            store = StateStore(Path(temp), SeededClock(NOW.isoformat()))
            fetcher = _FakeFetcher()
            fetcher.overrides[url] = FetchResponse(200, {"Content-Type": "application/json"}, body, url)
            policy = _single_seed_policy({
                "kind": "github_releases", "url": url,
                "provenance": "generic fixture", "interest": "releases",
                "cadence_minutes": 360,
            })
            first = collect_public_feeds(policy, SeededClock(NOW.isoformat()), store=store, fetcher=fetcher)
            self.assertEqual(first[0].status, "ok")
            self.assertEqual(len(first[0].items), 1)
            carried = collect_public_feeds(
                policy, SeededClock("2026-10-04T19:00:00+08:00"), store=store, fetcher=fetcher)
            self.assertEqual(carried[0].status, "ok")
            self.assertTrue(carried[0].cached)
            self.assertEqual(len(carried[0].items), 1)
            self.assertEqual(len(fetcher.calls), 1)


if __name__ == "__main__":
    unittest.main()
