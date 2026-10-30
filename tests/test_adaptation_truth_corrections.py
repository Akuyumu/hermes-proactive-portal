"""Adversarial corrections for per-source truth and typed config proposals.

Synthetic only: dictionaries, a seeded clock and an in-memory store. No network,
no real state, no configuration write and no credentials.
"""
from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import config_proposals as C  # noqa: E402
from proactive_pilot import source_truth as T  # noqa: E402

NOW = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)


def iso(delta: int) -> str:
    return (NOW - timedelta(minutes=delta)).isoformat()


class _Store:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def load(self, key: str, default: object) -> object:
        return copy.deepcopy(self.data.get(key, default))

    def save(self, key: str, value: object) -> None:
        self.data[key] = copy.deepcopy(value)


class TruthCorrections(unittest.TestCase):
    def rows(self, rec, health=None):
        registry = {"sources": {"a": {"id": "a", "kind": "web_page",
                                      "cadence_minutes": 360, **rec}}}
        return T.ordinary_source_rows(registry, health or {}, now=NOW,
                                      stale_minutes=45)

    def test_future_timestamps_cannot_be_fresh(self):
        self.assertFalse(T._age_ok(iso(-60), NOW, 360))
        row = self.rows({"cache_at": iso(-60), "cache_items": []})[0]
        self.assertFalse(row["fresh"])
        self.assertFalse(row["healthy"])
        success = self.rows({}, {"feed:a": {
            "last_observation_kind": "success",
            "last_success_at": iso(-60),
        }})[0]
        self.assertFalse(success["fresh"])
        self.assertFalse(success["healthy"])

    def test_valid_cadence_cache_is_healthy_including_quiet_feed(self):
        row = self.rows({"cache_at": iso(60), "cache_items": []})[0]
        self.assertTrue(row["fresh"])
        self.assertTrue(row["healthy"])
        self.assertEqual(row["status"], T.STATUS_CACHED)
        self.assertEqual(T.aggregate([row]), "complete")

    def test_structured_cache_expiry_overrides_recent_cache_at(self):
        rec = {
            "cache_at": iso(5),
            "cache_items": [{
                "category": "weather_forecast",
                "valid_from": iso(120),
                "valid_until": iso(1),
            }],
        }
        row = self.rows(rec)[0]
        self.assertFalse(row["fresh"])
        self.assertFalse(row["healthy"])
        self.assertEqual(row["status"], T.STATUS_STALE)

    def test_explicit_stale_reason_overrides_recent_cache_at(self):
        row = self.rows({"cache_at": iso(5), "cache_items": [],
                         "stale_reason": "feed expired/stale"})[0]
        self.assertFalse(row["fresh"])
        self.assertFalse(row["healthy"])

    def test_transient_failure_without_evidence_is_unknown_not_fresh(self):
        row = self.rows(
            {"stale_reason": "timeout"},
            {"feed:a": {"last_observation_kind": "failure",
                         "failure_class": "retryable", "failure_streak": 5}},
        )[0]
        self.assertFalse(row["fresh"])
        self.assertFalse(row["healthy"])
        self.assertEqual(row["status"], T.STATUS_UNVERIFIED)

    def test_transient_failure_with_valid_cache_is_cached_and_current(self):
        row = self.rows({"cache_at": iso(5), "cache_items": [],
                         "stale_reason": "timeout"})[0]
        self.assertTrue(row["fresh"])
        self.assertTrue(row["healthy"])
        self.assertEqual(row["status"], T.STATUS_CACHED)

    def test_terminal_failure_overrides_valid_cache(self):
        row = self.rows(
            {"cache_at": iso(5), "cache_items": [],
             "stale_reason": "feed expired/stale"},
            {"feed:a": {"last_observation_kind": "failure",
                         "failure_class": "other", "failure_streak": 2}},
        )[0]
        self.assertFalse(row["fresh"])
        self.assertFalse(row["healthy"])

    def test_lta_gate_skip_is_neutral_not_complete(self):
        row = self.rows({"kind": "lta_train_alerts",
                         "stale_reason": "intentional transport gate skip"})[0]
        self.assertFalse(row["fresh"])
        self.assertFalse(row["healthy"])
        self.assertNotEqual(T.aggregate([row]), "complete")
        cached = self.rows({"kind": "lta_train_alerts", "cache_at": iso(5),
                            "cache_items": [],
                            "stale_reason": "intentional transport gate skip"})[0]
        self.assertTrue(cached["healthy"])

    def test_per_source_cadence_beats_global_freshness(self):
        row = self.rows({"cadence_minutes": 10, "cache_at": iso(20),
                         "cache_items": []})[0]
        self.assertFalse(row["healthy"])
        self.assertFalse(row["fresh"])

    def test_malformed_cache_is_never_healthy(self):
        row = self.rows({"cache_at": iso(60), "cache_items": "not-a-list"})[0]
        self.assertFalse(row["healthy"])
        self.assertFalse(row["fresh"])
        malformed = self.rows({"cache_at": "not-a-timestamp",
                               "cache_items": []})[0]
        self.assertFalse(malformed["healthy"])

    def test_parked_and_disabled_sources_are_skipped(self):
        registry = {"sources": {
            "a": {"id": "a", "parked": True, "cache_at": iso(5),
                  "cache_items": []},
            "b": {"id": "b", "disabled": True, "cache_at": iso(5),
                  "cache_items": []},
            "c": {"id": "c", "cache_at": iso(5), "cache_items": []},
        }}
        rows = T.ordinary_source_rows(registry, {}, now=NOW, stale_minutes=45)
        self.assertEqual([row["id"] for row in rows], ["c"])

    def test_aggregate_mixed_health_is_partial_never_complete(self):
        healthy = {"healthy": True, "status": T.STATUS_FRESH}
        for other in ({"healthy": False, "status": T.STATUS_UNVERIFIED},
                      {"healthy": False, "status": T.STATUS_STALE},
                      {"healthy": False, "status": T.STATUS_UNSUPPORTED}):
            self.assertEqual(T.aggregate([healthy, other]), "partial")
        self.assertEqual(T.aggregate([]), "unknown")

    def test_adaptive_rows_match_real_coverage_fields(self):
        coverage = {"watches": {"w": {
            "last_attempt_at": iso(5),
            "sources": [
                {"status": "success", "checked_at": iso(5), "error": "",
                 "kind": "reddit_search", "interest": "x", "count": 0},
                {"status": "skipped", "checked_at": "", "error": "",
                 "reason": "deferred by per-run cap"},
                {"status": "failed", "checked_at": iso(5), "error": "timeout"},
                {"status": "unsupported", "checked_at": "",
                 "error": "", "reason": "scope has no supported public route"},
            ],
        }}}
        rows = T.adaptive_source_rows(coverage, now=NOW, stale_minutes=45)
        self.assertEqual(rows[0]["status"], T.STATUS_FRESH)
        self.assertTrue(rows[0]["healthy"])
        self.assertFalse(rows[1]["fresh"])
        self.assertFalse(rows[1]["healthy"])
        self.assertFalse(rows[2]["fresh"])
        self.assertFalse(rows[2]["healthy"])
        self.assertEqual(rows[3]["status"], T.STATUS_UNSUPPORTED)
        self.assertEqual(T.aggregate(rows), "partial")

    def test_public_truth_mixed_registry_is_partial(self):
        config = {"sources": {"public_feeds": True}, "public_interests": ["x"]}
        registry = {"sources": {
            "a": {"id": "a", "cache_at": iso(5), "cache_items": []},
            "b": {"id": "b", "stale_reason": "timeout"},
        }}
        result = T.public_truth(config, {}, registry=registry, now=NOW)
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["status"], T.STATUS_PARTIAL)
        self.assertFalse(result["fresh"])

    def test_legacy_public_truth_rejects_future_success(self):
        config = {"sources": {"public_feeds": True}, "public_interests": ["x"]}
        state = {"source_health": {"sources": {
            "feed:a": {"last_success_at": iso(-60)},
        }}}
        result = T.public_truth(config, state, now=NOW)
        self.assertFalse(result["fresh"])
        self.assertEqual(result["status"], T.STATUS_STALE)


class ProposalCorrections(unittest.TestCase):
    def observe(self, store, failures, at, *, cadence=None, key=60):
        rec = {"id": "x", "failures": failures, "stale_reason": "http 429"}
        if cadence is not None:
            rec["cadence_minutes"] = cadence
        return C.observe(
            store, {"feed_cadence_minutes": key},
            public_registry={"sources": {"x": rec}}, now=at,
        )

    def test_failure_count_churn_does_not_reannounce(self):
        store = _Store()
        self.observe(store, 3, NOW)
        pid, _ = C.pending_notice(store, NOW)
        C.mark_presented(store, pid, NOW)
        self.observe(store, 4, NOW + timedelta(hours=1))
        self.assertIsNone(C.pending_notice(store, NOW + timedelta(hours=1)))

    def test_dismissal_survives_evidence_churn(self):
        store = _Store()
        self.observe(store, 3, NOW)
        pid, _ = C.pending_notice(store, NOW)
        C.dismiss(store, pid, NOW)
        self.observe(store, 4, NOW + timedelta(hours=1))
        self.assertIsNone(C.pending_notice(store, NOW + timedelta(hours=1)))

    def test_presented_proposal_is_not_rearmed_by_expiry(self):
        store = _Store()
        self.observe(store, 3, NOW)
        pid, _ = C.pending_notice(store, NOW)
        C.mark_presented(store, pid, NOW)
        later = NOW + timedelta(days=10)
        self.observe(store, 3, later)
        self.assertIsNone(C.pending_notice(store, later))

    def test_explicit_source_cadence_gets_no_ineffective_global_proposal(self):
        store = _Store()
        self.assertEqual(self.observe(store, 3, NOW, cadence=360), [])
        self.assertIsNone(C.pending_notice(store, NOW))

    def test_proposal_carries_path_risk_authority_and_exact_id(self):
        store = _Store()
        policy = {"feed_cadence_minutes": 60}
        created = self.observe(store, 3, NOW)
        self.assertEqual(len(created), 1)
        proposal = created[0]
        self.assertEqual(proposal["settings_path"], "advanced.legacy_policy.feed_cadence_minutes")
        self.assertTrue(proposal["risk"])
        self.assertIn("foreground", proposal["authority"])
        self.assertTrue(proposal["id"].startswith("cfg-"))
        self.assertEqual(policy["feed_cadence_minutes"], 60)

    def test_changed_config_closes_obsolete_pending_proposal(self):
        store = _Store()
        old = self.observe(store, 3, NOW)[0]
        self.observe(store, 3, NOW + timedelta(hours=1), key=90)
        registry = C.load_registry(store)
        self.assertEqual(registry["proposals"][old["id"]]["status"], "superseded")
        notice = C.pending_notice(
            store, NOW + timedelta(hours=1),
            policy={"feed_cadence_minutes": 90})
        self.assertIsNotNone(notice)
        self.assertNotEqual(notice[0], old["id"])

    def test_relevance_is_rechecked_before_notice(self):
        store = _Store()
        pid, _ = C.pending_notice(store, NOW) or (None, None)
        self.assertIsNone(pid)
        self.observe(store, 3, NOW)
        self.assertIsNone(C.pending_notice(
            store, NOW, policy={"feed_cadence_minutes": 90}))

    def test_fixed_source_issue_withdraws_pending_proposal(self):
        store = _Store()
        created = self.observe(store, 3, NOW)[0]
        self.observe(store, 0, NOW + timedelta(hours=1))
        registry = C.load_registry(store)
        self.assertEqual(registry["proposals"][created["id"]]["status"], "withdrawn")
        self.assertIsNone(C.pending_notice(store, NOW + timedelta(hours=1)))

    def test_show_and_handoff_never_apply_configuration(self):
        store = _Store()
        pid = self.observe(store, 3, NOW)[0]["id"]
        shown = C.show(store, pid, NOW)
        self.assertEqual(shown["id"], pid)
        policy = {"feed_cadence_minutes": 60}
        result = C.approval_handoff(store, pid, policy, now=NOW)
        self.assertEqual(result["id"], pid)
        self.assertEqual(result["settings_path"], "advanced.legacy_policy.feed_cadence_minutes")
        self.assertEqual(result["change"], {"feed_cadence_minutes": 120})
        self.assertFalse(result["applied"])
        self.assertEqual(policy["feed_cadence_minutes"], 60)
        self.assertIsNone(C.show(store, "cfg-missing", NOW))

    def test_handoff_rejects_wrong_id_stale_before_and_expiry(self):
        store = _Store()
        pid = self.observe(store, 3, NOW)[0]["id"]
        with self.assertRaises(ValueError):
            C.approval_handoff(store, "cfg-missing",
                               {"feed_cadence_minutes": 60}, now=NOW)
        with self.assertRaises(ValueError):
            C.approval_handoff(store, pid,
                               {"feed_cadence_minutes": 90}, now=NOW)

        expiring = _Store()
        expiring_id = self.observe(expiring, 3, NOW)[0]["id"]
        with self.assertRaises(ValueError):
            C.approval_handoff(expiring, expiring_id,
                               {"feed_cadence_minutes": 60},
                               now=NOW + timedelta(days=30))


if __name__ == "__main__":
    unittest.main(verbosity=2)
