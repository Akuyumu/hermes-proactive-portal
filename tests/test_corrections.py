"""Independent regression tests for the parent inspection corrections.

Run with the bootstrapped interpreter (pytest not required):
  TMPDIR=<scratch> python3 tests/test_corrections.py
Exercises the real consumer functions (decide/record_delivery/build_packet).
No network, no production sources, no message is sent.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.candidates import build_packet, candidate_signature  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult, _count_tools  # noqa: E402
from proactive_pilot.gates import decide, deferred_coverage_notice, record_delivery  # noqa: E402
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 10, 0, tzinfo=SG)


def _clock(at=NOW):
    class C:
        def now(self):
            return at

    return C()


def _policy():
    p = json.loads(json.dumps(DEFAULT_POLICY))
    p["_preferences"] = {"explicit": [], "inferred": [], "snoozes": []}
    return p


def _packet(*, start=("+45", "concrete"), coverage_status="ok", checked_offset_min=0, all_day=False, version="v1"):
    now = NOW
    if start[0] is None:
        date = ""
    else:
        date = (now + timedelta(minutes=int(start[0]))).isoformat()
    checked = (now + timedelta(minutes=checked_offset_min)).isoformat()
    return {
        "schema": 1,
        "generated_at": now.isoformat(),
        "coverage": {"mail:formal": {"status": coverage_status, "checked_at": checked}},
        "candidates": [{
            "kind": "mail", "source": "mail:formal", "ref": "mail:formal:one",
            "version": version, "subject": "Newsletter", "date": date, "all_day": all_day,
        }],
    }


def _proposal(**kw):
    args = dict(kind="mail", key="mail:formal:one", message="A newsletter arrived.",
                urgency="normal", evidence=["mail:formal:one"])
    args.update(kw)
    return EvaluatorResult("notify", **args)


def test_fabricated_urgent_reference_alone_rejects():
    dec = decide(_proposal(urgency="urgent"), _packet(), clock=_clock(), policy=_policy(), prefs={}, state={})
    assert not dec["send"] and not dec["message"]


def test_wellformed_urgent_without_source_time_rejects():
    dec = decide(
        _proposal(urgency="urgent", consequence="lose slot", confidence="high",
                  deadline=(NOW + timedelta(minutes=30)).isoformat()),
        _packet(start=(None, "none")), clock=_clock(), policy=_policy(), prefs={}, state={},
    )
    assert not dec["send"]


def test_expired_rejects():
    dec = decide(_proposal(expiry=(NOW - timedelta(hours=1)).isoformat()),
                 _packet(), clock=_clock(), policy=_policy(), prefs={}, state={})
    assert not dec["send"] and dec["reason"] == "expired"


def test_malformed_and_naive_expiry_reject():
    bad = decide(_proposal(expiry="not-a-time"), _packet(), clock=_clock(), policy=_policy(), prefs={}, state={})
    naive = decide(_proposal(expiry="2099-01-01T00:00:00"), _packet(), clock=_clock(), policy=_policy(), prefs={}, state={})
    assert not bad["send"] and not naive["send"]


def test_paraphrase_dedupe_stable():
    packet = _packet()
    state = {}
    first = decide(_proposal(message="Alert: a newsletter arrived."), packet,
                   clock=_clock(), policy=_policy(), prefs={}, state=state)
    assert first["send"]
    record_delivery(state, first, packet, _clock(), _policy())
    later = _clock(NOW + timedelta(minutes=40))
    second = decide(_proposal(message="Alert: your newsletter is here."), packet,
                    clock=later, policy=_policy(), prefs={}, state=state)
    assert not second["send"] and second["reason"] == "duplicate"


def test_urgent_distinct_event_bypasses_normal_cooldown():
    packet = _packet(start=("+30", "concrete"))
    state = {"last_delivery_at": (NOW - timedelta(minutes=5)).isoformat(),
             "delivered": [{"at": (NOW - timedelta(minutes=5)).isoformat(), "signature": "x", "urgent": False}]}
    prop = _proposal(urgency="urgent", consequence="miss appointment", confidence="high",
                     deadline=(NOW + timedelta(minutes=30)).isoformat())
    dec = decide(prop, packet, clock=_clock(), policy=_policy(), prefs={}, state=state)
    assert dec["send"] and dec["urgent"]


def test_stale_coverage_never_urgency():
    prop = _proposal(urgency="urgent", consequence="miss appointment", confidence="high",
                     deadline=(NOW + timedelta(minutes=30)).isoformat())
    dec = decide(prop, _packet(checked_offset_min=-120), clock=_clock(), policy=_policy(), prefs={}, state={})
    assert not dec["send"]


def test_truncated_collection_is_partial():
    trace = SourceResult("mail:formal", "ok", count=15, items=[], checked_at=NOW.isoformat(), truncated=True)
    packet = build_packet([trace], clock=_clock(), policy=_policy(), baseline={})
    assert packet["coverage"]["mail:formal"]["status"] == "partial"
    assert packet["coverage"]["mail:formal"]["truncated"] is True
    healthy = build_packet([SourceResult("mail:formal", "ok", count=1, items=[], checked_at=NOW.isoformat())],
                           clock=_clock(), policy=_policy(), baseline={})
    assert healthy["coverage"]["mail:formal"]["status"] == "ok"


def test_quiet_coverage_notice_deferred_not_lost():
    results = [SourceResult("calendar:primary", "error", error="HTTP 503", checked_at=NOW.isoformat())]
    state = {}
    quiet = dict(_policy(), quiet_start_hour=0, quiet_end_hour=23)
    assert deferred_coverage_notice(results, state, quiet, NOW) is None
    assert "failed_sources" not in state  # not consumed while suppressed
    at_wake = dict(_policy(), quiet_start_hour=0, quiet_end_hour=8)
    assert deferred_coverage_notice(results, state, at_wake, NOW) is not None


def test_tool_count_negative_control():
    assert _count_tools('prose mentioning "type": "tool_use" should not count') == 0
    assert _count_tools('{"type": "tool_use", "name": "shell"}') == 1


def test_signature_ignores_wording_but_tracks_version_and_phase():
    a = candidate_signature({"kind": "mail", "ref": "r", "version": "1", "phase": "p"})
    b = candidate_signature({"kind": "mail", "ref": "r", "version": "1", "phase": "p", "subject": "different"})
    c = candidate_signature({"kind": "mail", "ref": "r", "version": "2", "phase": "p"})
    d = candidate_signature({"kind": "mail", "ref": "r", "version": "1", "phase": "q"})
    assert a == b and a != c and a != d


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
    print(f"\n{passed}/{len(tests)} correction tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
