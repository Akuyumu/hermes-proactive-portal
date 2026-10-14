"""Agentic, fail-closed transport gate tests for the credentialed LTA source.

No network and no model: the plugin model lane and the credentialed transport are
both injected fakes. Covered: the candidate pre-filter (no facts -> zero model and
zero LTA calls), yes/no verdicts, the interval / daily-cap / standing-disruption
floors, model-lane unavailability, the silent non-alerting skip representation,
and the absence of any credential value in evidence/telemetry/exception text.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.clock import SG, SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.policy import load_policy  # noqa: E402
from proactive_pilot.publicfeeds import (  # noqa: E402
    CredentialMissing,
    FetchResponse,
    LTA_TRAIN_ALERTS_URL,
    collect_public_feeds,
)
from proactive_pilot.source_health import (  # noqa: E402
    OBSERVATION_NON_EVIDENTIAL,
    classify_observation,
)
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.transport import (  # noqa: E402
    LTA_DISRUPTION_RETRY_MINUTES,
    LTA_GATE_STATE_NAME,
    LTA_MAX_CALLS_PER_DAY,
    LTA_MIN_INTERVAL_MINUTES,
    TransportGate,
    build_transport_facts,
    evaluate_transport_gate,
    save_transport_state,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
FAKE_KEY = "FAKE-LTA-KEY-SENTINEL-do-not-use"


class _Lane:
    """Injected plugin model lane fake (never names a provider/model)."""

    def __init__(self, *, verdict=True, available=True, obj=None, boom=False):
        self.available = available
        self.calls = 0
        self.boom = boom
        self._obj = obj if obj is not None else {
            "travel_likely": verdict, "reason": "commute window"}

    def complete_json(self, **kwargs):
        self.calls += 1
        if self.boom:
            raise RuntimeError("lane down")
        return self._obj


def _facts(**kwargs):
    base = {"now": NOW, "results": None, "location_state": None,
            "location_fix_at": None, "registry": None}
    base.update(kwargs)
    return build_transport_facts(**base)


def _event(**over):
    row = {
        "title": "Lecture",
        "start": (NOW + timedelta(minutes=45)).isoformat(),
        "end": (NOW + timedelta(minutes=105)).isoformat(),
        "location": "Near campus",
    }
    row.update(over)
    return row


def _calendar_result(*items):
    return [SourceResult("calendar:test", "ok", items=list(items))]


def _lta_policy():
    policy = load_policy(None)
    policy["feed_seed_sources"] = [{
        "kind": "lta_train_alerts",
        "url": LTA_TRAIN_ALERTS_URL,
        "provenance": "test seed",
        "interest": "lta",
        "cadence_minutes": 60,
    }]
    return policy


class TransportGateTests(unittest.TestCase):

    # -- pre-filter ---------------------------------------------------------

    def test_quiet_day_makes_no_model_and_no_lta_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / LTA_GATE_STATE_NAME
            lane = _Lane()
            facts = _facts()
            self.assertFalse(facts.has_candidate())
            decision, _state = evaluate_transport_gate(
                now=NOW, facts=facts, state_path=state_path, model_lane=lane)
            self.assertFalse(decision.allow)
            self.assertEqual(decision.verdict, "skip-no-clue")
            self.assertEqual(lane.calls, 0)

    def test_candidate_event_alone_is_a_candidate(self):
        facts = _facts(results=_calendar_result(_event()))
        self.assertTrue(facts.has_candidate())
        self.assertEqual(len(facts.events), 1)

    def test_event_beyond_lookahead_is_not_a_candidate(self):
        far = _event(start=(NOW + timedelta(hours=7)).isoformat())
        facts = _facts(results=_calendar_result(far))
        self.assertFalse(facts.has_candidate())

    def test_fresh_location_is_a_candidate_and_stale_is_not(self):
        self.assertTrue(_facts(location_fix_at=(NOW - timedelta(minutes=30)).isoformat())
                        .has_candidate())
        self.assertFalse(_facts(location_fix_at=(NOW - timedelta(hours=3)).isoformat())
                         .has_candidate())

    def test_standing_disruption_is_a_candidate(self):
        registry = {"sources": {"x": {"kind": "lta_train_alerts", "cache_items": [
            {"live_service_alert": True, "all_clear": False}]}}}
        facts = _facts(registry=registry)
        self.assertTrue(facts.disruption_standing)
        self.assertTrue(facts.has_candidate())

    # -- verdicts -----------------------------------------------------------

    def test_candidate_and_yes_allows(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / LTA_GATE_STATE_NAME
            lane = _Lane(verdict=True)
            facts = _facts(results=_calendar_result(_event()))
            decision, _state = evaluate_transport_gate(
                now=NOW, facts=facts, state_path=state_path, model_lane=lane)
            self.assertTrue(decision.allow)
            self.assertEqual(decision.verdict, "yes")
            self.assertEqual(lane.calls, 1)

    def test_candidate_and_no_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / LTA_GATE_STATE_NAME
            lane = _Lane(verdict=False)
            facts = _facts(results=_calendar_result(_event()))
            decision, _state = evaluate_transport_gate(
                now=NOW, facts=facts, state_path=state_path, model_lane=lane)
            self.assertFalse(decision.allow)
            self.assertEqual(decision.verdict, "no")

    def test_non_boolean_yes_is_not_a_clean_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / LTA_GATE_STATE_NAME
            for obj in ({"travel_likely": "yes"}, {"travel_likely": 1},
                        {"likely": True}, {}):
                lane = _Lane(obj=obj)
                decision, _state = evaluate_transport_gate(
                    now=NOW, facts=_facts(results=_calendar_result(_event())),
                    state_path=state_path, model_lane=lane)
                self.assertFalse(decision.allow, obj)
                self.assertEqual(decision.verdict, "no", obj)

    # -- floors -------------------------------------------------------------

    def test_second_call_within_twenty_minutes_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / LTA_GATE_STATE_NAME
            lane = _Lane()
            facts = _facts(results=_calendar_result(_event()))
            gate = TransportGate(facts=facts, model_lane=lane, state_path=state_path)
            first = gate.decide(now=NOW)
            self.assertTrue(first.allow)
            self.assertEqual(lane.calls, 1)
            # 5 minutes later: refused by the 20-minute floor, no extra model call.
            second = gate.decide(now=NOW + timedelta(minutes=5))
            self.assertFalse(second.allow)
            self.assertEqual(second.verdict, "skip-floor")
            self.assertEqual(lane.calls, 1)
            # 21 minutes later: permitted again.
            third = gate.decide(now=NOW + timedelta(minutes=21))
            self.assertTrue(third.allow)
            self.assertEqual(lane.calls, 2)

    def test_daily_cap_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / LTA_GATE_STATE_NAME
            save_transport_state(state_path, {
                "schema": 1,
                "last_call_at": (NOW - timedelta(hours=6)).isoformat(),
                "day": NOW.astimezone(SG).date().isoformat(),
                "calls_today": LTA_MAX_CALLS_PER_DAY,
                "history": [],
            })
            lane = _Lane()
            decision, _state = evaluate_transport_gate(
                now=NOW, facts=_facts(results=_calendar_result(_event())),
                state_path=state_path, model_lane=lane)
            self.assertFalse(decision.allow)
            self.assertEqual(decision.verdict, "skip-floor")
            self.assertEqual(lane.calls, 0)

    def test_standing_disruption_allows_fifteen_minute_retry_but_respects_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / LTA_GATE_STATE_NAME
            registry = {"sources": {"x": {"kind": "lta_train_alerts", "cache_items": [
                {"live_service_alert": True, "all_clear": False}]}}}
            facts = _facts(registry=registry)
            save_transport_state(state_path, {
                "schema": 1,
                "last_call_at": (NOW - timedelta(minutes=16)).isoformat(),
                "day": NOW.astimezone(SG).date().isoformat(),
                "calls_today": 1,
                "history": [],
            })
            lane = _Lane()
            decision, _state = evaluate_transport_gate(
                now=NOW, facts=facts, state_path=state_path, model_lane=lane)
            self.assertTrue(decision.allow, decision.reason)
            self.assertLess(LTA_DISRUPTION_RETRY_MINUTES, LTA_MIN_INTERVAL_MINUTES)
            # Still under the daily cap requirement.
            save_transport_state(state_path, {
                "schema": 1,
                "last_call_at": (NOW - timedelta(minutes=16)).isoformat(),
                "day": NOW.astimezone(SG).date().isoformat(),
                "calls_today": LTA_MAX_CALLS_PER_DAY,
                "history": [],
            })
            lane2 = _Lane()
            capped, _state = evaluate_transport_gate(
                now=NOW, facts=facts, state_path=state_path, model_lane=lane2)
            self.assertFalse(capped.allow)
            self.assertEqual(lane2.calls, 0)

    # -- fail closed on the model lane --------------------------------------

    def test_model_lane_unavailable_makes_no_lta_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / LTA_GATE_STATE_NAME
            for lane in (None, _Lane(available=False)):
                decision, _state = evaluate_transport_gate(
                    now=NOW, facts=_facts(results=_calendar_result(_event())),
                    state_path=state_path, model_lane=lane)
                self.assertFalse(decision.allow)
                self.assertEqual(decision.verdict, "skip-model")

    def test_model_lane_error_makes_no_lta_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / LTA_GATE_STATE_NAME
            lane = _Lane(boom=True)
            decision, _state = evaluate_transport_gate(
                now=NOW, facts=_facts(results=_calendar_result(_event())),
                state_path=state_path, model_lane=lane)
            self.assertFalse(decision.allow)
            self.assertEqual(decision.verdict, "skip-model")
            self.assertTrue(decision.model_called)

    # -- security: no credential anywhere -----------------------------------

    def test_no_credential_value_in_telemetry_or_decision_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / LTA_GATE_STATE_NAME
            lane = _Lane()
            gate = TransportGate(facts=_facts(results=_calendar_result(_event())),
                                 model_lane=lane, state_path=state_path)
            decision = gate.decide(now=NOW)
            self.assertNotIn(FAKE_KEY, json.dumps(decision.reason))
            raw = state_path.read_text(encoding="utf-8")
            self.assertNotIn(FAKE_KEY, raw)
            self.assertNotIn("lat", raw.lower())


class TransportGateCollectionTests(unittest.TestCase):
    """End-to-end through collect_public_feeds with an injected fakes."""

    def _collect(self, gate, counting):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))

            def unused_fetcher(*a, **k):
                raise AssertionError("generic fetcher must not be used for lta")

            def cred(*, timeout=None, max_bytes=None):
                counting.append(1)
                return FetchResponse(200, {"Content-Type": "application/json"},
                                     b'{"value": {"Status": 1, "AffectedSegments": [], "Message": []}}',
                                     LTA_TRAIN_ALERTS_URL)

            results = collect_public_feeds(
                _lta_policy(), SeededClock(NOW.isoformat()), store=store,
                fetcher=unused_fetcher, credentialed_fetcher=cred,
                transport_gate=gate)
            return [r for r in results if r.name.startswith("feed:lta_train_alerts")]

    def _gate(self, tmp, *, facts, lane):
        return TransportGate(facts=facts, model_lane=lane,
                             state_path=Path(tmp) / LTA_GATE_STATE_NAME)

    def test_yes_makes_exactly_one_lta_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            lane = _Lane(verdict=True)
            gate = self._gate(tmp, facts=_facts(results=_calendar_result(_event())),
                              lane=lane)
            counting = []
            lta = self._collect(gate, counting)
            self.assertEqual(len(counting), 1)
            self.assertEqual(lane.calls, 1)
            self.assertTrue(lta and lta[0].healthy, lta[0].error if lta else "none")

    def test_no_makes_zero_lta_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            lane = _Lane(verdict=False)
            gate = self._gate(tmp, facts=_facts(results=_calendar_result(_event())),
                              lane=lane)
            counting = []
            lta = self._collect(gate, counting)
            self.assertEqual(counting, [])
            self.assertTrue(lta)
            self.assertEqual(lta[0].status, "deferred")

    def test_no_candidate_makes_zero_model_and_zero_lta_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            lane = _Lane()
            gate = self._gate(tmp, facts=_facts(), lane=lane)
            counting = []
            lta = self._collect(gate, counting)
            self.assertEqual(counting, [])
            self.assertEqual(lane.calls, 0)
            self.assertEqual(lta[0].status, "deferred")

    def test_skip_is_non_evidential_and_not_stale_or_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            lane = _Lane()
            gate = self._gate(tmp, facts=_facts(), lane=lane)
            counting = []
            lta = self._collect(gate, counting)
            self.assertTrue(lta)
            row = lta[0]
            self.assertNotEqual(row.status, "stale")
            self.assertNotEqual(row.status, "error")
            self.assertFalse(row.healthy)
            self.assertEqual(classify_observation(row), OBSERVATION_NON_EVIDENTIAL)
            self.assertIn("transport gate", row.failure)

    def test_key_missing_after_gate_yes_fails_closed_without_key_leak(self):
        # The gate permits, but the real credentialed path has no key: it must
        # fail closed with the non-secret diagnostic and no key value anywhere.
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))

            def unused_fetcher(*a, **k):
                raise AssertionError("generic fetcher must not be used for lta")

            gate = self._gate(tmp, facts=_facts(results=_calendar_result(_event())),
                              lane=_Lane(verdict=True))
            with mock.patch.dict(os.environ, {"LTA_DATAMALL_ACCOUNT_KEY": ""}):
                results = collect_public_feeds(
                    _lta_policy(), SeededClock(NOW.isoformat()), store=store,
                    fetcher=unused_fetcher, transport_gate=gate)
            lta = [r for r in results if r.name.startswith("feed:lta_train_alerts")]
            self.assertTrue(lta)
            text = json.dumps({"error": lta[0].error, "failure": lta[0].failure})
            self.assertNotIn(FAKE_KEY, text)
            self.assertIn("LTA_DATAMALL_ACCOUNT_KEY", text)


if __name__ == "__main__":
    unittest.main()
