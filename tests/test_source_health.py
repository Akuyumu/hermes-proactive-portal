"""Per-source health gate: durable incidents, bounded recovery, honest alerts.

Synthetic only: fake collectors/result objects and a seeded clock. No network,
no production source, no message is sent.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot import source_health as sh  # noqa: E402
from proactive_pilot.userconfig import validate_config, policy_overrides  # noqa: E402

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 10, 0, tzinfo=SG)


def _policy(**over):
    p = dict(DEFAULT_POLICY)
    p.update(over)
    return p


def _fail(name="mail:formal", error="HTTP 503", **kw):
    return SourceResult(name, "error", error=error, checked_at=NOW.isoformat(), **kw)


def _ok(name="mail:formal", **kw):
    return SourceResult(name, "ok", count=0, checked_at=NOW.isoformat(), **kw)


class ClassificationTests(unittest.TestCase):
    def test_healthy_ok_is_success(self):
        self.assertEqual(sh.classify_observation(_ok()), sh.OBSERVATION_SUCCESS)

    def test_error_is_failure(self):
        self.assertEqual(sh.classify_observation(_fail()), sh.OBSERVATION_FAILURE)

    def test_truncated_ok_is_non_evidential(self):
        self.assertEqual(sh.classify_observation(_ok(truncated=True)),
                         sh.OBSERVATION_NON_EVIDENTIAL)

    def test_cached_is_non_evidential(self):
        self.assertEqual(sh.classify_observation(_ok(cached=True, cache_at=NOW.isoformat())),
                         sh.OBSERVATION_NON_EVIDENTIAL)

    def test_deferred_and_busy_non_evidential(self):
        self.assertEqual(sh.classify_observation(_fail(error="source budget-deferred")),
                         sh.OBSERVATION_NON_EVIDENTIAL)
        self.assertEqual(sh.classify_observation(SourceResult("x", "busy")),
                         sh.OBSERVATION_NON_EVIDENTIAL)

    def test_failure_classes(self):
        self.assertEqual(sh.classify_failure("HTTP 503"), sh.FAILURE_RETRYABLE)
        self.assertEqual(sh.classify_failure("timeout"), sh.FAILURE_RETRYABLE)
        self.assertEqual(sh.classify_failure("429 too many requests"), sh.FAILURE_RATE_LIMITED)
        self.assertEqual(sh.classify_failure("401 unauthorized"), sh.FAILURE_OWNER_ACTION)


class IncidentTests(unittest.TestCase):
    def test_attempted_only_counts(self):
        state: dict = {}
        sh.record_observations(state, [_fail()], NOW, _policy())
        rec = state["source_health"]["sources"]["mail:formal"]
        self.assertEqual(rec["attempted_checks"], 1)
        self.assertEqual(rec["failure_streak"], 1)
        # A deferred observation is not evidence: counters unchanged.
        before = dict(rec)
        sh.record_observations(state, [_fail(error="budget-deferred")], NOW, _policy())
        rec = state["source_health"]["sources"]["mail:formal"]
        self.assertEqual(rec["attempted_checks"], before["attempted_checks"])
        self.assertEqual(rec["failure_streak"], before["failure_streak"])

    def test_truncated_ok_does_not_recover(self):
        state: dict = {}
        for _ in range(3):
            sh.record_observations(state, [_fail()], NOW, _policy())
        sh.record_observations(state, [_ok(truncated=True)], NOW, _policy())
        rec = state["source_health"]["sources"]["mail:formal"]
        self.assertEqual(rec["failure_streak"], 3)

    def test_success_resets_and_new_incident(self):
        state: dict = {}
        for _ in range(3):
            sh.record_observations(state, [_fail()], NOW, _policy())
        incident = state["source_health"]["sources"]["mail:formal"]["incident_id"]
        sh.record_observations(state, [_ok()], NOW, _policy())
        rec = state["source_health"]["sources"]["mail:formal"]
        self.assertEqual(rec["failure_streak"], 0)
        self.assertNotEqual(rec["incident_id"], incident)


class DispatchGateTests(unittest.TestCase):
    def test_below_threshold_dispatches(self):
        state: dict = {}
        sh.record_observations(state, [_fail()], NOW, _policy())
        gate = sh.dispatch_gate(state, "mail:formal", NOW, _policy())
        self.assertTrue(gate["dispatch"])

    def test_open_cooldown_blocks_then_half_open_one_probe(self):
        state: dict = {}
        p = _policy(source_health_cooldown_minutes=60)
        for _ in range(3):
            sh.record_observations(state, [_fail()], NOW, p)
        # Immediately after opening, dispatch is blocked by the cooldown.
        blocked = sh.dispatch_gate(state, "mail:formal", NOW, p)
        self.assertFalse(blocked["dispatch"])
        self.assertEqual(blocked["mode"], "open")
        self.assertTrue(blocked["next_check_at"])
        # After the cooldown exactly one half-open probe is permitted.
        later = NOW + timedelta(minutes=61)
        probe = sh.dispatch_gate(state, "mail:formal", later, p)
        self.assertTrue(probe["dispatch"])
        self.assertTrue(probe["probe"])
        self.assertEqual(probe["mode"], "half_open")
        # A second call inside the new window must NOT issue a second probe.
        again = sh.dispatch_gate(state, "mail:formal", later + timedelta(minutes=1), p)
        self.assertFalse(again["dispatch"])

    def test_429_honours_retry_after(self):
        state: dict = {}
        p = _policy(source_health_cooldown_minutes=5)
        for _ in range(3):
            sh.record_observations(state, [_fail(error="429 rate limited; Retry-After: 3600")], NOW, p)
        blocked = sh.dispatch_gate(state, "mail:formal", NOW + timedelta(minutes=10), p)
        self.assertFalse(blocked["dispatch"])
        allowed = sh.dispatch_gate(state, "mail:formal", NOW + timedelta(minutes=61), p)
        self.assertTrue(allowed["dispatch"])


class AlertTests(unittest.TestCase):
    def _chronic(self, policy, error="HTTP 503"):
        state: dict = {}
        t = NOW
        for _ in range(3):
            sh.record_observations(state, [_fail(error=error)], t, policy)
        # make the incident look already persistent
        state["source_health"]["sources"]["mail:formal"]["first_failure_at"] = (
            t - timedelta(hours=3)).isoformat()
        later = t + timedelta(minutes=61)
        sh.dispatch_gate(state, "mail:formal", later, policy)
        sh.record_observations(state, [_fail(error=error)], later, policy)
        later2 = later + timedelta(minutes=61)
        sh.dispatch_gate(state, "mail:formal", later2, policy)
        sh.record_observations(state, [_fail(error=error)], later2, policy)
        return state, later2

    def test_noncritical_persistent_is_silent(self):
        state, now = self._chronic(_policy())
        self.assertIsNone(sh.health_alert(state, now, _policy(), commit=False))

    def test_transient_flap_silent(self):
        state: dict = {}
        p = _policy(health_critical_sources=["mail:formal"])
        for _ in range(2):
            sh.record_observations(state, [_fail()], NOW, p)
        self.assertIsNone(sh.health_alert(state, NOW, p, commit=False))

    def test_critical_persistent_alerts_once(self):
        p = _policy(health_critical_sources=["mail:formal"])
        state, now = self._chronic(p, error="401 unauthorized")
        first = sh.health_alert(state, now, p, commit=True)
        self.assertIsNotNone(first)
        self.assertIsNone(sh.health_alert(state, now + timedelta(minutes=1), p, commit=True))
        # One alert per unresolved incident even across a reloaded state blob.
        reloaded = {"source_health": state["source_health"]}
        self.assertIsNone(sh.health_alert(reloaded, now + timedelta(minutes=2), p, commit=True))

    def test_critical_persistent_overload_has_no_owner_action(self):
        p = _policy(health_critical_sources=["mail:formal"])
        state, now = self._chronic(p)
        self.assertIsNone(sh.health_alert(state, now, p, commit=False))

    def test_owner_action_wording(self):
        p = _policy(health_critical_sources=["mail:formal"])
        state: dict = {}
        t = NOW
        for _ in range(3):
            sh.record_observations(state, [_fail(error="401 unauthorized")], t, p)
        state["source_health"]["sources"]["mail:formal"]["first_failure_at"] = (
            t - timedelta(hours=3)).isoformat()
        later = t + timedelta(minutes=61)
        sh.dispatch_gate(state, "mail:formal", later, p)
        sh.record_observations(state, [_fail(error="401 unauthorized")], later, p)
        later2 = later + timedelta(minutes=61)
        sh.dispatch_gate(state, "mail:formal", later2, p)
        sh.record_observations(state, [_fail(error="401 unauthorized")], later2, p)
        msg = sh.health_alert(state, later2, p, commit=True)
        self.assertIn("credentials", msg.lower())

    def test_corrupt_state_fails_safe(self):
        p = _policy(health_critical_sources=["mail:formal"])
        state = {"source_health": {"schema": 999, "sources": "not-a-dict"}}
        self.assertIsNone(sh.health_alert(state, NOW, p, commit=False))
        # A corrupt gate fails OPEN (ordinary checks keep working) rather than
        # blocking every source forever.
        gate = sh.dispatch_gate(state, "mail:formal", NOW, p)
        self.assertTrue(gate["dispatch"])


class RuntimeGuardTests(unittest.TestCase):
    def test_routine_health_rejected(self):
        self.assertIsNotNone(sh.routine_health_reason("Coverage note: mail:formal could not be read just now."))
        self.assertIsNotNone(sh.routine_health_reason("Sources are having trouble; will keep retrying."))

    def test_business_warning_allowed(self):
        self.assertIsNone(sh.routine_health_reason("Your invoice payment is due today."))
        self.assertIsNone(sh.routine_health_reason("RSVP deadline for the event expires at 18:00."))


class ConfigMappingTests(unittest.TestCase):
    def test_critical_sources_default_neutral(self):
        cfg = validate_config({})
        self.assertEqual(cfg["health"]["critical_sources"], [])
        self.assertEqual(policy_overrides(cfg)["health_critical_sources"], [])

    def test_explicit_critical_sources_mapped(self):
        cfg = validate_config({"health": {"critical_sources": ["mail:formal", "calendar:primary"]}})
        self.assertEqual(cfg["health"]["critical_sources"], ["mail:formal", "calendar:primary"])
        self.assertEqual(policy_overrides(cfg)["health_critical_sources"],
                         ["mail:formal", "calendar:primary"])

    def test_mail_calendar_not_critical_by_default(self):
        p = _policy()
        self.assertFalse(sh.is_critical_source(p, "mail:formal"))
        self.assertFalse(sh.is_critical_source(p, "calendar:primary"))

    def test_policy_defaults_are_bounded(self):
        p = _policy()
        self.assertGreaterEqual(p["source_health_failure_threshold"], 3)
        self.assertGreaterEqual(p["source_health_persistent_minutes"], 120)


if __name__ == "__main__":
    unittest.main(verbosity=2)
