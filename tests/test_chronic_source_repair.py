import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from proactive_pilot import interest_feedback
from proactive_pilot import source_health as sh
from proactive_pilot import source_maintenance as sm

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
OLD_URL = "https://old.example.com/feed.xml"
NEW_URL = "https://new.example.com/feed.xml"


class SyntheticResult:
    def __init__(
        self,
        *,
        name="feed:old",
        status="ok",
        count=1,
        items=None,
        cached=False,
        truncated=False,
        error="",
        healthy=False,
        attempted=True,
        checked_at=None,
    ):
        self.name = name
        self.status = status
        self.count = count
        self.items = items if items is not None else [
            {"title": "synthetic item", "date": NOW.isoformat()}
        ]
        self.cached = cached
        self.truncated = truncated
        self.error = error
        self.healthy = healthy
        self.attempted = attempted
        self.checked_at = checked_at or NOW.isoformat()


class SyntheticDecider:
    available = True

    def __init__(self, decision):
        self.decision = decision
        self.calls = []
        self.residual = None

    def set_residual(self, seconds):
        self.residual = seconds

    def __call__(self, rec, candidate, result):
        self.calls.append((rec, candidate, result))
        return dict(self.decision)


def usable_result():
    return SyntheticResult()


def equivalent_decision():
    return {
        "equivalent": True,
        "purpose": "same synthetic public topic",
        "authority": "reputable_public",
        "date_scope_ok": True,
        "reason": "synthetic equivalence double",
    }


def candidate():
    return {
        "kind": "rss",
        "url": NEW_URL,
        "interest": "synthetic topic",
        "provenance": "synthetic discovery double",
    }


def make_policy(**overrides):
    policy = {
        "public_source_maintenance_enabled": True,
        "adaptive_broad_discovery_enabled": True,
        "source_replacement_failure_threshold": 3,
        "source_replacement_max_attempts": 3,
        "source_replacement_max_probes_per_cycle": 1,
        "source_replacement_cooldown_minutes": 360,
        "source_replacement_max_sources_per_cycle": 2,
    }
    policy.update(overrides)
    return policy


def make_registry(**overrides):
    rec = {
        "id": "feed:old",
        "kind": "rss",
        "url": OLD_URL,
        "host": "old.example.com",
        "interest": "synthetic topic",
        "failures": 3,
        "stale_reason": "timeout",
        "maintenance": {},
    }
    rec.update(overrides)
    return {"sources": {"feed:old": rec}}


def run_attempt(registry, *, policy=None, decider=None, probe=None, candidates=None):
    search_calls = []

    def fake_search(rec, registry_arg, policy_arg, **kwargs):
        search_calls.append(
            {"rec": rec, "registry": registry_arg, "policy": policy_arg, "kwargs": kwargs}
        )
        return list(candidates if candidates is not None else [candidate()]), {
            "status": "ok",
            "reason": "synthetic search double",
        }

    probe_calls = []

    def fake_probe(kind, url, intent):
        probe_calls.append((kind, url, intent))
        return usable_result() if probe is None else probe(kind, url, intent)

    fake_decider = decider if decider is not None else SyntheticDecider(equivalent_decision())
    with mock.patch.object(sm, "search_alternatives", fake_search), mock.patch.object(
        interest_feedback, "watch_allowed", return_value=True
    ):
        adopted = sm.attempt_replacements(
            registry,
            policy=policy or make_policy(),
            now=NOW,
            probe=fake_probe,
            decider=fake_decider,
        )
    return adopted, search_calls, probe_calls, fake_decider


class TestChronicSourceRepair(unittest.TestCase):
    def test_isolated_timeout_stays_quiet(self):
        registry = make_registry(failures=1)
        adopted, search_calls, probe_calls, decider = run_attempt(registry)

        self.assertEqual(adopted, [])
        self.assertEqual(search_calls, [])
        self.assertEqual(probe_calls, [])
        self.assertEqual(decider.calls, [])
        self.assertEqual(registry["sources"]["feed:old"]["maintenance"], {})

    def test_repeated_timeout_enters_existing_verified_replacement_path(self):
        registry = make_registry(failures=3, stale_reason="temporarily unavailable")
        adopted, search_calls, probe_calls, decider = run_attempt(registry)

        self.assertEqual(len(search_calls), 1)
        self.assertEqual(len(probe_calls), 1)
        self.assertEqual(len(decider.calls), 1)
        self.assertEqual(len(adopted), 1)
        event = adopted[0]
        self.assertEqual(event["old_id"], "feed:old")
        self.assertEqual(event["new_url"], NEW_URL)
        self.assertTrue(registry["sources"]["feed:old"]["parked"])
        new_rec = registry["sources"][event["new_id"]]
        self.assertEqual(new_rec["replacement_decision"]["authority"], "reputable_public")
        self.assertTrue(new_rec["cache_items"])

    def test_neutral_deferrals_remain_noops(self):
        reasons = [
            "budget-deferred",
            "cadence (60m)",
            "deferred by source-health cooldown",
            "skipped: per-run limit",
            "cached (cadence)",
            "partial response",
            "busy",
            "missing",
            "truncated body",
            "not modified (304)",
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                registry = make_registry(failures=5, stale_reason=reason)
                before = dict(registry["sources"]["feed:old"]["maintenance"])
                adopted, search_calls, probe_calls, decider = run_attempt(registry)

                self.assertEqual(adopted, [])
                self.assertEqual(search_calls, [])
                self.assertEqual(probe_calls, [])
                self.assertEqual(decider.calls, [])
                self.assertEqual(registry["sources"]["feed:old"]["maintenance"], before)

    def test_rate_limits_never_churn(self):
        reasons = [
            "429 rate limited; Retry-After: 60",
            "Retry-After: 60",
            "rate limit",
            "too many requests",
            "quota exceeded",
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                registry = make_registry(failures=9, stale_reason=reason)
                adopted, search_calls, probe_calls, decider = run_attempt(registry)

                self.assertEqual(adopted, [])
                self.assertEqual(search_calls, [])
                self.assertEqual(probe_calls, [])
                self.assertEqual(decider.calls, [])
                self.assertFalse(registry["sources"]["feed:old"].get("parked", False))

    def test_credential_failures_never_churn(self):
        reasons = [
            "401 unauthorized",
            "authorization revoked",
            "missing configured grant",
            "credential expired",
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                registry = make_registry(failures=9, stale_reason=reason)
                adopted, search_calls, probe_calls, decider = run_attempt(registry)

                self.assertEqual(adopted, [])
                self.assertEqual(search_calls, [])
                self.assertEqual(probe_calls, [])
                self.assertEqual(decider.calls, [])
                self.assertFalse(registry["sources"]["feed:old"].get("parked", False))

    def test_no_fresh_probe_evidence_means_no_adoption(self):
        cases = [
            ("cached", lambda: SyntheticResult(cached=True)),
            ("stale", lambda: SyntheticResult(status="stale", items=[])),
            ("empty", lambda: SyntheticResult(count=0, items=[])),
            ("error", lambda: SyntheticResult(error="cached body")),
        ]
        for label, result_factory in cases:
            with self.subTest(case=label):
                registry = make_registry(failures=3, stale_reason="timeout")
                adopted, search_calls, probe_calls, decider = run_attempt(
                    registry, probe=lambda *a: result_factory()
                )

                self.assertEqual(adopted, [])
                self.assertEqual(len(search_calls), 1)
                self.assertEqual(len(probe_calls), 1)
                self.assertEqual(decider.calls, [])
                self.assertFalse(registry["sources"]["feed:old"].get("parked", False))
                outcome = registry["sources"]["feed:old"]["maintenance"]["last_outcome"]
                self.assertEqual(outcome["status"], "unusable_evidence")

    def test_no_equivalence_decision_means_no_adoption(self):
        registry = make_registry(failures=3, stale_reason="timeout")
        rejected = dict(equivalent_decision(), equivalent=False, reason="synthetic rejection")
        adopted, search_calls, probe_calls, decider = run_attempt(
            registry, decider=SyntheticDecider(rejected)
        )

        self.assertEqual(adopted, [])
        self.assertEqual(len(search_calls), 1)
        self.assertEqual(len(probe_calls), 1)
        self.assertEqual(len(decider.calls), 1)
        self.assertFalse(registry["sources"]["feed:old"].get("parked", False))
        outcome = registry["sources"]["feed:old"]["maintenance"]["last_outcome"]
        self.assertEqual(outcome["status"], "not_equivalent")

    def test_malformed_persisted_evidence_fails_closed(self):
        cases = [
            ("string failures", {"failures": "3"}),
            ("none failures", {"failures": None}),
            ("string attempts", {"maintenance": {"attempts": "1"}}),
            ("negative attempts", {"maintenance": {"attempts": -1}}),
            ("non-dict maintenance", {"maintenance": "malformed"}),
            ("non-string reason", {"stale_reason": ["timeout"]}),
            ("empty reason", {"stale_reason": ""}),
        ]
        for label, overrides in cases:
            with self.subTest(case=label):
                registry = make_registry(**overrides)
                adopted, search_calls, probe_calls, decider = run_attempt(registry)

                self.assertEqual(adopted, [])
                self.assertEqual(search_calls, [])
                self.assertEqual(probe_calls, [])
                self.assertEqual(decider.calls, [])

    def test_nonfinite_policy_bounds_fail_closed(self):
        self.assertIsNone(
            sm._policy_counter({"bound": float("inf")}, "bound", 3, minimum=1)
        )
        self.assertIsNone(
            sm._policy_counter({"bound": float("nan")}, "bound", 3, minimum=1)
        )
        self.assertIsNone(
            sm._policy_counter({"bound": float("-inf")}, "bound", 3, minimum=1)
        )
        cases = [
            ("inf threshold", {"source_replacement_failure_threshold": float("inf")}),
            ("nan threshold", {"source_replacement_failure_threshold": float("nan")}),
            ("inf max attempts", {"source_replacement_max_attempts": float("inf")}),
            ("nan max attempts", {"source_replacement_max_attempts": float("nan")}),
        ]
        for label, overrides in cases:
            with self.subTest(case=label):
                registry = make_registry(failures=9, stale_reason="timeout")
                adopted, search_calls, probe_calls, decider = run_attempt(
                    registry, policy=make_policy(**overrides)
                )

                self.assertEqual(adopted, [])
                self.assertEqual(search_calls, [])
                self.assertEqual(probe_calls, [])
                self.assertEqual(decider.calls, [])

    def test_publicfeeds_post_success_state_is_not_eligible(self):
        registry = make_registry(failures=0)
        registry["sources"]["feed:old"].pop("stale_reason", None)
        adopted, search_calls, probe_calls, decider = run_attempt(registry)

        self.assertEqual(adopted, [])
        self.assertEqual(search_calls, [])
        self.assertEqual(probe_calls, [])
        self.assertEqual(decider.calls, [])

    def test_source_health_success_resets_health_failure_streak(self):
        policy = {
            "source_health_failure_threshold": 3,
            "source_health_cooldown_minutes": 60,
        }
        state = {}
        failure = SyntheticResult(status="error", error="timeout", attempted=True)

        for minutes in (0, 1, 2):
            sh.record_observations(state, [failure], NOW + timedelta(minutes=minutes), policy)

        record = state["source_health"]["sources"]["feed:old"]
        self.assertEqual(record["failure_streak"], 3)
        self.assertEqual(record["failure_class"], sh.FAILURE_RETRYABLE)

        success = SyntheticResult(status="ok", healthy=True, attempted=True)
        sh.record_observations(state, [success], NOW + timedelta(minutes=3), policy)

        record = state["source_health"]["sources"]["feed:old"]
        self.assertEqual(record["failure_streak"], 0)
        self.assertEqual(record["failure_class"], "")
        self.assertEqual(record["recovery_state"], sh._STATE_CLOSED)


if __name__ == "__main__":
    unittest.main()
