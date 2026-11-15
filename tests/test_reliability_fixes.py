"""Negative controls for the proactive-pilot reliability repair.

Every behaviour changed in this staged copy has a negative control here: the
test fails if the defect is reintroduced. All fixtures are synthetic; no network,
no production source, no message is sent. Run with:

  TMPDIR=<scratch> python -m unittest discover -s tests -p 'test_reliability_*.py'
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.candidates import (  # noqa: E402
    _cap_candidates,
    build_packet,
    is_unchanged_task_list,
)
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import (  # noqa: E402
    SourceResult,
    _attach_previews,
    _parse_time,
    collect_himalaya,
)
from proactive_pilot.evaluator import EvaluatorResult  # noqa: E402
from proactive_pilot.gates import (  # noqa: E402
    EVALUATOR_FAILURE_NOTICE,
    coverage_notice,
    deferred_coverage_notice,
    note_evaluator_outcome,
    notice_allowed,
)
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 10, 0, tzinfo=SG)


def _policy(**over):
    p = json.loads(json.dumps(DEFAULT_POLICY))
    p["timezone"] = "Asia/Singapore"
    p["_preferences"] = {"explicit": [], "inferred": [], "snoozes": []}
    p.update(over)
    return p


def _clock(at=NOW):
    class C:
        def now(self):
            return at

    return C()


class _Runner:
    def __init__(self, envelopes, bodies=None):
        self.envelopes = envelopes
        self.bodies = bodies or {}
        self.reads: list[str] = []

    def run(self, argv, timeout):
        if "envelope" in argv:
            return 0, json.dumps({"envelopes": self.envelopes}), ""
        if "read" in argv:
            mid = argv[argv.index("read") + 1]
            self.reads.append(mid)
            return 0, self.bodies.get(mid, "routine project notes"), ""
        return 1, "", "unexpected"


class MailDateTests(unittest.TestCase):
    def test_rfc2822_fallback_parsed(self):
        parsed = _parse_time("Fri, 03 Oct 2026 09:00:00 +0800")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo is not None, True)

    def test_iso_still_parsed(self):
        self.assertIsNotNone(_parse_time("2026-10-03T09:00:00Z"))
        self.assertIsNotNone(_parse_time("2026-10-03"))

    def test_invalid_date_is_none(self):
        self.assertIsNone(_parse_time("definitely not a date"))
        self.assertIsNone(_parse_time(""))

    def test_invalid_mail_date_marks_partial(self):
        runner = _Runner([{"id": "x", "subject": "Hi", "from": [{"email": "a@b"}],
                           "date": "Wed, 32 Foo 9999 99:99", "has-attachment": False}])
        res = collect_himalaya("formal", 15, clock=_clock(), window_hours=48, runner=runner)
        self.assertTrue(res.truncated)
        packet = build_packet([res], clock=_clock(), policy=_policy(), baseline={})
        self.assertEqual(packet["coverage"]["mail:formal"]["status"], "partial")

    def test_valid_mail_date_not_partial(self):
        runner = _Runner([{"id": "x", "subject": "Hi", "from": [{"email": "a@b"}],
                           "date": "Fri, 03 Oct 2026 09:00:00 +0800", "has-attachment": False}])
        res = collect_himalaya("formal", 15, clock=_clock(), window_hours=48, runner=runner)
        self.assertFalse(res.truncated)
        self.assertEqual(res.count, 1)


class PreviewBudgetTests(unittest.TestCase):
    def _items(self):
        return [
            {"id": "otp1", "sensitive": True, "preview": ""},
            {"id": "n1", "sensitive": False, "preview": ""},
            {"id": "n2", "sensitive": False, "preview": ""},
        ]

    def test_sensitive_not_counted_against_budget(self):
        runner = _Runner([])
        _attach_previews("formal", self._items(), runner, 30, preview_limit=1, preview_chars=120)
        self.assertEqual(runner.reads, ["n1"])

    def test_budget_bounds_eligible_attempts(self):
        runner = _Runner([])
        _attach_previews("formal", self._items(), runner, 30, preview_limit=2, preview_chars=120)
        self.assertEqual(runner.reads, ["n1", "n2"])

    def test_all_sensitive_reads_nothing(self):
        runner = _Runner([])
        items = [{"id": "o", "sensitive": True, "preview": ""}]
        _attach_previews("formal", items, runner, 30, preview_limit=5, preview_chars=120)
        self.assertEqual(runner.reads, [])


class FairCapTests(unittest.TestCase):
    def test_tasks_do_not_starve_mail(self):
        cands = [{"kind": "tasks", "ref": f"t{i}"} for i in range(10)] + [{"kind": "mail", "ref": "m1"}]
        picked, dropped = _cap_candidates(cands, 3)
        kinds = {c["kind"] for c in picked}
        self.assertIn("mail", kinds)
        self.assertIn("tasks", kinds)
        self.assertEqual(len(dropped), 8)

    def test_critical_seated_first(self):
        cands = [
            {"kind": "tasks", "ref": "far1"},
            {"kind": "mail", "ref": "m1"},
            {"kind": "calendar", "ref": "cal1", "phase": "within30min"},
        ]
        picked, _ = _cap_candidates(cands, 1)
        self.assertEqual(picked[0]["ref"], "cal1")

    def test_all_fit_no_omission(self):
        cands = [{"kind": "mail", "ref": "a"}, {"kind": "mail", "ref": "b"}]
        picked, dropped = _cap_candidates(cands, 5)
        self.assertEqual(len(picked), 2)
        self.assertEqual(dropped, [])

    def test_packet_marks_starved_source_partial(self):
        results = []
        for i in range(6):
            results.append(SourceResult("tasks:primary", "ok", count=1, items=[{
                "ref": f"task:p:t{i}", "title": f"t{i}", "due_date": "2026-10-20", "status": "needsAction",
            }], checked_at=NOW.isoformat()))
        results.append(SourceResult("mail:formal", "ok", count=1, items=[{
            "ref": "mail:formal:m1", "subject": "Important", "date": NOW.isoformat(),
        }], checked_at=NOW.isoformat()))
        packet = build_packet(results, clock=_clock(), policy=_policy(max_packet_candidates=3),
                              baseline={})
        self.assertEqual(packet["coverage"]["tasks:primary"]["status"], "partial")


class TransactionalNoticeTests(unittest.TestCase):
    def _failing(self):
        return [SourceResult("calendar:primary", "error", error="HTTP 503", checked_at=NOW.isoformat())]

    def test_uncommitted_notice_leaves_state_untouched(self):
        state: dict = {}
        msg = coverage_notice(self._failing(), state, _policy(), commit=False)
        self.assertIsNotNone(msg)
        self.assertNotIn("failed_sources", state)

    def test_uncommitted_notice_retried(self):
        state: dict = {}
        first = coverage_notice(self._failing(), state, _policy(), commit=False)
        second = coverage_notice(self._failing(), state, _policy(), commit=False)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)

    def test_committed_notice_deduped(self):
        state: dict = {}
        self.assertIsNotNone(coverage_notice(self._failing(), state, _policy(), commit=True))
        self.assertIsNone(coverage_notice(self._failing(), state, _policy(), commit=True))

    def test_deferred_quiet_does_not_consume(self):
        state: dict = {}
        quiet = _policy(quiet_start_hour=0, quiet_end_hour=23)
        self.assertIsNone(deferred_coverage_notice(self._failing(), state, quiet, NOW, commit=False))
        self.assertNotIn("failed_sources", state)

    def test_recovery_does_not_reenable_flap(self):
        # fail -> (uncommitted, suppressed) -> recover -> fail: the original
        # failure is still "new" because it was never consumed, so no stampede.
        state: dict = {}
        coverage_notice(self._failing(), state, _policy(), commit=False)
        healthy = [SourceResult("calendar:primary", "ok", count=0, checked_at=NOW.isoformat())]
        coverage_notice(healthy, state, _policy(), commit=True)
        again = coverage_notice(self._failing(), state, _policy(), commit=True)
        self.assertIsNotNone(again)
        self.assertIsNone(coverage_notice(self._failing(), state, _policy(), commit=True))


class CoverageCooldownTests(unittest.TestCase):
    """Per-source cooldown stops A/B/A flapping without permanent suppression."""

    def test_flap_does_not_renotify_within_cooldown(self):
        state: dict = {}
        p = _policy()
        a = [SourceResult("mail:formal", "error", error="x", checked_at=NOW.isoformat())]
        b = [SourceResult("calendar:primary", "error", error="x", checked_at=NOW.isoformat())]
        self.assertIsNotNone(coverage_notice(a, state, p, now=NOW, commit=True))
        self.assertIsNotNone(coverage_notice(b, state, p, now=NOW, commit=True))
        # A fails again immediately: still cooling down, so no second A notice.
        self.assertIsNone(coverage_notice(a, state, p, now=NOW, commit=True))

    def test_genuine_later_loss_surfaces_after_cooldown(self):
        state: dict = {}
        p = _policy(coverage_notice_cooldown_minutes=30)
        a = [SourceResult("mail:formal", "error", error="x", checked_at=NOW.isoformat())]
        healthy = [SourceResult("mail:formal", "ok", count=0, checked_at=NOW.isoformat())]
        self.assertIsNotNone(coverage_notice(a, state, p, now=NOW, commit=True))
        coverage_notice(healthy, state, p, now=NOW, commit=True)  # recovers
        self.assertIsNone(coverage_notice(a, state, p, now=NOW, commit=True))  # still cooling
        later = NOW + timedelta(minutes=31)
        self.assertIsNotNone(coverage_notice(a, state, p, now=later, commit=True))

    def test_cooldown_state_is_bounded(self):
        state: dict = {}
        p = _policy()
        a = [SourceResult("mail:formal", "error", error="x", checked_at=NOW.isoformat())]
        coverage_notice(a, state, p, now=NOW, commit=True)
        self.assertIn("coverage_notified_at", state)
        blob = json.dumps(state)
        self.assertNotIn("x", blob)  # no raw error text persisted


class EvaluatorFailureMessageTests(unittest.TestCase):
    def test_message_is_factual_not_paused(self):
        low = EVALUATOR_FAILURE_NOTICE.lower()
        self.assertNotIn("paused", low)
        self.assertNotIn("no action needed", low)
        self.assertIn("retry", low)
        self.assertIn("miss", low)

    def test_needs_evaluator_notice_reports_armed(self):
        from proactive_pilot.gates import needs_evaluator_notice

        # While quiet the notice cannot emit but stays armed...
        state: dict = {}
        quiet = _policy(quiet_start_hour=0, quiet_end_hour=23)
        for _ in range(3):
            note_evaluator_outcome(EvaluatorResult("error"), state, quiet, {"explicit": []}, NOW)
        self.assertTrue(needs_evaluator_notice(state, quiet))
        # ...and once emitted it is no longer armed.
        note_evaluator_outcome(EvaluatorResult("error"), state, _policy(), {"explicit": []}, NOW)
        self.assertFalse(needs_evaluator_notice(state, _policy()))


class NoticeGuardTests(unittest.TestCase):
    def test_quiet_blocks(self):
        ok, reason = notice_allowed({}, _policy(quiet_start_hour=0, quiet_end_hour=23),
                                    {"explicit": []}, NOW)
        self.assertFalse(ok)
        self.assertEqual(reason, "quiet hours")

    def test_pause_blocks(self):
        prefs = {"explicit": [], "snoozes": [], "paused": {"active": True}}
        ok, reason = notice_allowed({}, _policy(), prefs, NOW)
        self.assertFalse(ok)
        self.assertEqual(reason, "snoozed")

    def test_cooldown_blocks(self):
        state = {"last_delivery_at": (NOW - timedelta(minutes=5)).isoformat()}
        ok, reason = notice_allowed(state, _policy(), {"explicit": []}, NOW)
        self.assertFalse(ok)
        self.assertEqual(reason, "cooldown")

    def test_cap_blocks(self):
        state = {"delivered": [{"at": NOW.isoformat(), "signature": s} for s in ("a", "b", "c", "d", "e")]}
        ok, reason = notice_allowed(state, _policy(daily_normal_cap=5), {"explicit": []}, NOW)
        self.assertFalse(ok)
        self.assertEqual(reason, "daily cap")

    def test_clear_blocks_nothing(self):
        ok, reason = notice_allowed({}, _policy(), {"explicit": []}, NOW)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")


class EvaluatorFailureTests(unittest.TestCase):
    def _fail(self, state, policy, prefs, now=NOW):
        return note_evaluator_outcome(EvaluatorResult("error"), state, policy, prefs, now)

    def test_notice_only_after_repeated_failures(self):
        state: dict = {}
        p = _policy()
        self.assertIsNone(self._fail(state, p, {"explicit": []}))
        self.assertIsNone(self._fail(state, p, {"explicit": []}))
        self.assertEqual(self._fail(state, p, {"explicit": []}), EVALUATOR_FAILURE_NOTICE)

    def test_notice_is_bounded(self):
        state: dict = {}
        p = _policy()
        for _ in range(3):
            self._fail(state, p, {"explicit": []})
        for _ in range(4):
            self.assertIsNone(self._fail(state, p, {"explicit": []}))

    def test_success_resets(self):
        state: dict = {}
        p = _policy()
        for _ in range(3):
            self._fail(state, p, {"explicit": []})
        note_evaluator_outcome(EvaluatorResult("silence"), state, p, {"explicit": []}, NOW)
        self.assertEqual(state["evaluator_failures"]["count"], 0)
        self.assertIsNone(self._fail(state, p, {"explicit": []}))

    def test_suppressed_failure_stays_armed(self):
        state: dict = {}
        quiet = _policy(quiet_start_hour=0, quiet_end_hour=23)
        for _ in range(3):
            self.assertIsNone(self._fail(state, quiet, {"explicit": []}))
        self.assertEqual(state["evaluator_failures"]["notified_at"], "")
        # At waking hours the armed notice emerges once.
        awake = _policy()
        self.assertEqual(self._fail(state, awake, {"explicit": []}), EVALUATOR_FAILURE_NOTICE)

    def test_no_raw_error_or_body_persisted(self):
        state: dict = {}
        p = _policy()
        note_evaluator_outcome(EvaluatorResult("error", error="secret model dump qwerty"),
                               state, p, {"explicit": []}, NOW)
        blob = json.dumps(state)
        self.assertNotIn("secret", blob)
        self.assertNotIn("qwerty", blob)


class BaselineSkipTests(unittest.TestCase):
    def _packet(self, **cand):
        base = {"kind": "tasks", "ref": "task:p:t1", "version": "v1", "status": "needsAction",
                "due_date": "2026-10-20", "phase": "far"}
        base.update(cand)
        return {"candidates": [base], "generated_at": "2026-10-03T10:00:00+08:00"}

    def _base(self):
        return {"baseline_tasks": {"task:p:t1": {"version": "v1", "status": "needsAction",
                                                  "due_date": "2026-10-20", "phase": "far"}}}

    def test_unchanged_is_skippable(self):
        self.assertTrue(is_unchanged_task_list(self._packet(), self._base()))

    def test_changed_version_wakes(self):
        self.assertFalse(is_unchanged_task_list(self._packet(version="v2"), self._base()))

    def test_changed_status_wakes(self):
        self.assertFalse(is_unchanged_task_list(self._packet(status="completed"), self._base()))

    def test_changed_due_wakes(self):
        self.assertFalse(is_unchanged_task_list(self._packet(due_date="2026-10-25"), self._base()))

    def test_changed_phase_wakes(self):
        self.assertFalse(is_unchanged_task_list(self._packet(phase="overdue"), self._base()))

    def test_critical_phase_wakes_even_with_ref_only(self):
        p = {"candidates": [{"kind": "tasks", "ref": "task:p:t1", "version": "v1",
                             "status": "needsAction", "due_date": "2026-10-20",
                             "phase": "approaching"}],
             "generated_at": "2026-10-03T10:00:00+08:00"}
        self.assertFalse(is_unchanged_task_list(p, {"baseline_task_refs": ["task:p:t1"]}))

    def test_new_task_wakes(self):
        p = {"candidates": [{"kind": "tasks", "ref": "task:p:t2", "version": "v1",
                             "status": "needsAction", "due_date": "2026-10-20", "phase": "far"}],
             "generated_at": "2026-10-03T10:00:00+08:00"}
        self.assertFalse(is_unchanged_task_list(p, self._base()))


class DiagnosticSkipTests(unittest.TestCase):
    """Explicit diagnostics live on stderr; stdout stays deliverable-or-empty."""

    def _run(self, *args):
        import subprocess
        import os

        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run(
                [sys.executable, "-m", "proactive_pilot.main", "--state-dir", tmp, *args],
                capture_output=True, text=True, env=env, timeout=120, cwd=str(ROOT),
            )

    def test_check_diagnostic_on_stderr_only(self):
        # Explicit check may emit a diagnostic, but never on stdout.
        proc = self._run("--check", "--fixture", "healthy_empty")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", "check must not print a deliverable to stdout")
        body = json.loads(proc.stderr)
        self.assertEqual(body["command"], "check")
        self.assertNotIn("message", body)

    def test_dry_run_diagnostic_on_stderr_only(self):
        proc = self._run("--dry-run", "--fixture", "changed_event")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", "dry-run must not print a deliverable to stdout")
        body = json.loads(proc.stderr)
        self.assertEqual(body["diagnostic"], "dry-run")
        self.assertTrue(body["no_emission"])
        self.assertNotIn("message", body)

    def test_unchanged_skip_is_silent_on_stdout(self):
        import subprocess
        import os

        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        with tempfile.TemporaryDirectory() as tmp:
            first = subprocess.run(
                [sys.executable, "-m", "proactive_pilot.main", "--state-dir", tmp,
                 "--fixture", "healthy_empty"],
                capture_output=True, text=True, env=env, timeout=120, cwd=str(ROOT),
            )
            second = subprocess.run(
                [sys.executable, "-m", "proactive_pilot.main", "--state-dir", tmp,
                 "--fixture", "healthy_empty"],
                capture_output=True, text=True, env=env, timeout=120, cwd=str(ROOT),
            )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        # Production default --run: an unchanged skip is empty stdout, no debug.
        self.assertEqual(second.stdout, "", "unchanged run must not spam the scheduler")

    def test_unchanged_skip_diagnostic_when_checking(self):
        import subprocess
        import os

        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(2):
                second = subprocess.run(
                    [sys.executable, "-m", "proactive_pilot.main", "--state-dir", tmp,
                     "--check", "--fixture", "healthy_empty"],
                    capture_output=True, text=True, env=env, timeout=120, cwd=str(ROOT),
                )
        self.assertEqual(second.returncode, 0, second.stderr)
        body = json.loads(second.stderr)
        self.assertEqual(body["command"], "check")


if __name__ == "__main__":
    unittest.main(verbosity=2)
