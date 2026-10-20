"""Standalone test file for the staged proactive pilot.

Runs with any Python 3 (pytest is not required):
  python3 tests/test_proactive_pilot.py

Exercises real functions with negative controls. No network, no production data.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.candidates import build_packet, candidate_signature, is_unchanged_task_list  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.evaluator import (  # noqa: E402
    EvaluatorResult,
    _extract_json,
    fixture_packet,
    validate_object,
)
from proactive_pilot.fixtures import fixture_evaluator, fixture_results  # noqa: E402
from proactive_pilot.gates import coverage_notice, decide, record_delivery  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY, in_quiet_hours, preference_blocks  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402


def _policy():
    p = json.loads(json.dumps(DEFAULT_POLICY))
    p["timezone"] = "Asia/Singapore"
    p["_preferences"] = {"explicit": [], "inferred": [], "snoozes": []}
    return p


def test_healthy_empty_is_silent():
    clock = SeededClock("2026-10-02T20:00:00")
    policy = _policy()
    _, packet = (lambda r: (r, build_packet(r, clock=clock, policy=policy, baseline={})))(fixture_results("healthy_empty", clock))
    prop = fixture_evaluator("healthy_empty").evaluate(packet, policy)
    dec = decide(prop, packet, clock=clock, policy=policy, prefs=policy["_preferences"], state={})
    assert dec["send"] is False


def test_urgent_with_evidence_sends_at_night():
    clock = SeededClock("2026-10-02T03:00:00")
    policy = _policy()
    results = fixture_results("urgent_calendar", clock)
    packet = build_packet(results, clock=clock, policy=policy, baseline={})
    prop = fixture_evaluator("urgent_calendar").evaluate(packet, policy)
    dec = decide(prop, packet, clock=clock, policy=policy, prefs=policy["_preferences"], state={})
    assert dec["send"] and dec["urgent"]


def test_urgent_without_evidence_invalid():
    packet = fixture_packet("evt:primary:q1")
    obj = {"schema": 1, "action": "notify", "kind": "calendar", "key": "evt:primary:q1",
           "message": "URGENT you are late", "urgency": "urgent", "evidence": []}
    assert validate_object(obj, packet).action == "invalid"


def test_quiet_hours_suppress_normal():
    policy = _policy()
    clock = SeededClock("2026-10-02T03:00:00")
    results = fixture_results("changed_event", clock)
    packet = build_packet(results, clock=clock, policy=policy, baseline={})
    prop = fixture_evaluator("changed_event").evaluate(packet, policy)
    dec = decide(prop, packet, clock=clock, policy=policy, prefs=policy["_preferences"], state={})
    assert not dec["send"] and dec["reason"] == "quiet hours"


def test_dedupe_second_suppressed():
    policy = _policy()
    clock = SeededClock("2026-10-02T12:00:00")
    results = fixture_results("changed_event", clock)
    packet = build_packet(results, clock=clock, policy=policy, baseline={})
    prop = fixture_evaluator("changed_event").evaluate(packet, policy)
    state = {}
    d1 = decide(prop, packet, clock=clock, policy=policy, prefs=policy["_preferences"], state=state)
    d1.update({"kind": prop.kind, "key": prop.key, "expiry": prop.expiry})
    state = record_delivery(state, d1, packet, clock, policy)
    d2 = decide(prop, packet, clock=clock, policy=policy, prefs=policy["_preferences"], state=state)
    assert d1["send"] and not d2["send"] and d2["reason"] == "duplicate"


def test_hard_cap():
    policy = _policy()
    clock = SeededClock("2026-10-02T12:00:00")
    results = fixture_results("changed_event", clock)
    packet = build_packet(results, clock=clock, policy=policy, baseline={})
    prop = fixture_evaluator("changed_event").evaluate(packet, policy)
    state = {"delivered": [{"at": "2026-10-02T09:00:00+08:00", "signature": s, "urgent": False} for s in ("a", "b", "c", "d", "e")]}
    dec = decide(prop, packet, clock=clock, policy=policy, prefs=policy["_preferences"], state=state)
    assert not dec["send"] and dec["reason"] == "daily cap"


def test_coverage_notice_deduped():
    clock = SeededClock("2026-10-02T20:00:00")
    policy = _policy()
    results = fixture_results("failure", clock)
    state = {}
    assert coverage_notice(results, state, policy) is not None
    assert coverage_notice(results, state, policy) is None


def test_lock_prevents_overlap():
    clock = SeededClock("2026-10-02T20:00:00")
    with tempfile.TemporaryDirectory() as tmp:
        a = StateStore(Path(tmp), clock)
        b = StateStore(Path(tmp), clock)
        with a.lock() as first:
            assert first
            with b.lock() as second:
                assert not second


def test_malformed_json_invalid():
    assert validate_object({"nope": 1}, fixture_packet("x")).action == "invalid"
    assert _extract_json("not json") is None
    assert _extract_json('prefix {"schema":1,"action":"silence"}') == {"schema": 1, "action": "silence"}


def test_explicit_preference_blocks():
    clock = SeededClock("2026-10-02T20:00:00")
    prefs = {"explicit": [{"kind": "mail", "match": "newsletter", "suppress": True, "provenance": "user"}], "inferred": []}
    item = {"kind": "mail", "subject": "Weekly newsletter", "message": "Weekly newsletter"}
    assert preference_blocks(item, prefs, clock.now()) is True
    assert preference_blocks({"kind": "calendar", "subject": "x", "message": "x"}, prefs, clock.now()) is False


def test_inferred_preferences_never_block():
    clock = SeededClock("2026-10-02T20:00:00")
    prefs = {"explicit": [], "inferred": [{"kind": "mail", "match": "newsletter", "suppress": True}]}
    item = {"kind": "mail", "subject": "Weekly newsletter", "message": "Weekly newsletter"}
    assert preference_blocks(item, prefs, clock.now()) is False


def test_quiet_boundary():
    policy = _policy()
    assert in_quiet_hours(policy, SeededClock("2026-10-02T07:59:00").now())
    assert not in_quiet_hours(policy, SeededClock("2026-10-02T08:00:00").now())


def test_unchanged_tasks_shortcircuit():
    packet = {"candidates": [{"kind": "tasks", "ref": "task:p:t1"}]}
    assert is_unchanged_task_list(packet, {"baseline_task_refs": ["task:p:t1"]}) is True
    assert is_unchanged_task_list(packet, {"baseline_task_refs": []}) is False


def test_signature_stable_and_version_sensitive():
    a = candidate_signature({"kind": "calendar", "ref": "e", "version": "1", "subject": "s"})
    b = candidate_signature({"kind": "calendar", "ref": "e", "version": "1", "subject": "s"})
    c = candidate_signature({"kind": "calendar", "ref": "e", "version": "2", "subject": "s"})
    assert a == b and a != c


def test_state_files_owner_only():
    clock = SeededClock("2026-10-02T20:00:00")
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), clock)
        store.save("notify_state.json", {"schema": 1})
        mode = (Path(tmp) / "notify_state.json").stat().st_mode & 0o777
        assert mode == 0o600


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {test.__name__}: {exc!r}")
    print(f"\n{passed}/{len(tests)} tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
