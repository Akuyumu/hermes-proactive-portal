"""Regression + negative-control tests for the CPU-spin and false-healthy fixes.

Covers three repaired behaviours; each test fails if the defect is reintroduced:

1. ``candidates._kind_capped`` must terminate and hard-cap each kind. Before the
   fix, a single kind with more than ``max_candidates_per_kind`` items spun
   forever at 100% CPU (holding the shared run lock) because the bucket was
   never drained while ``while any(buckets.values())`` stayed true.
2. ``watchdog.run_deadline`` must abort a hung run, propagate through broad
   ``except Exception`` handlers, reap the hung child, and let the caller's
   ``StateStore.lock`` finally release the advisory lock.
3. The requested-reminder CLI must report a distinct, non-zero status on lock
   contention for explicit user mutations (``EXIT_BUSY``), while the scheduled
   dispatcher keeps a silent exit-0 busy skip so overlap never becomes native
   failure spam.
4. Nested deadlines must compose: the earliest deadline always wins, an inner
   longer per-source ``wallclock_alarm`` cannot mask an earlier outer
   ``run_deadline``, an expired outer fires on restoration, and the outer
   ``BaseException`` is not swallowed by a source's broad ``except Exception``.

All fixtures are synthetic; explicit fakes only; no live source, network, send,
job creation, credentials, or state. Temp dirs live under the workspace.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.candidates import _kind_capped, build_packet  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult, SourceTimeout, wallclock_alarm  # noqa: E402
from proactive_pilot.policy import load_policy  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.watchdog import RunDeadlineExceeded, run_deadline  # noqa: E402

CLOCK_ISO = "2026-10-03T10:00:00+08:00"


class _GuardExpired(Exception):
    """Test-only bound so a reintroduced spin fails the test instead of hanging."""


def _guard(signum: int, frame: object) -> None:  # noqa: ANN001, ARG001
    raise _GuardExpired()


def _bounded(callable_, seconds: float = 5.0):
    """Run ``callable_`` in the main thread under a hard test-only SIGALRM."""
    if not hasattr(signal, "SIGALRM"):
        return callable_()
    previous = signal.signal(signal.SIGALRM, _guard)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return callable_()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class KindCapSpinTests(unittest.TestCase):
    def test_single_kind_over_limit_terminates_and_is_capped(self):
        candidates = [{"kind": "mail", "ref": f"mail:{i}"} for i in range(61)]
        out = _bounded(lambda: _kind_capped(candidates, 60))
        self.assertEqual(len(out), 60)
        self.assertEqual([c["ref"] for c in out], [f"mail:{i}" for i in range(60)])

    def test_multiple_kinds_over_limit_terminate_each_capped(self):
        candidates = (
            [{"kind": "tasks", "ref": f"task:{i}"} for i in range(70)]
            + [{"kind": "mail", "ref": f"mail:{i}"} for i in range(70)]
        )
        out = _bounded(lambda: _kind_capped(candidates, 60))
        self.assertEqual(sum(1 for c in out if c["kind"] == "tasks"), 60)
        self.assertEqual(sum(1 for c in out if c["kind"] == "mail"), 60)

    def test_build_packet_over_limit_single_kind_terminates(self):
        clock = SeededClock(CLOCK_ISO)
        items = [
            {"ref": f"task:p:t{i}", "title": f"t{i}", "due_date": "2026-10-20",
             "status": "needsAction"}
            for i in range(61)
        ]
        result = SourceResult("tasks:primary", "ok", count=61, items=items,
                              checked_at=clock.now().isoformat())
        policy = load_policy(None)
        packet = _bounded(
            lambda: build_packet([result], clock=clock, policy=policy, baseline={}),
            seconds=10.0,
        )
        self.assertLessEqual(len(packet["candidates"]), 200)


class RunDeadlineTests(unittest.TestCase):
    def test_deadline_aborts_busy_loop_and_releases_lock(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(CLOCK_ISO))
            raised = False
            with store.lock() as acquired:
                self.assertTrue(acquired)
                try:
                    with run_deadline(0.3):
                        end = time.monotonic() + 5.0
                        while time.monotonic() < end:
                            pass
                except RunDeadlineExceeded:
                    raised = True
            self.assertTrue(raised, "whole-run deadline must abort a hung run")
            with store.lock() as reacquired:
                self.assertTrue(reacquired, "shared lock must be free after deadline")

    def test_deadline_is_baseexception_and_not_swallowed(self):
        self.assertFalse(issubclass(RunDeadlineExceeded, Exception))
        swallowed = False
        try:
            try:
                raise RunDeadlineExceeded("synthetic")
            except Exception:  # must NOT catch the whole-run deadline
                swallowed = True
        except RunDeadlineExceeded:
            pass
        self.assertFalse(swallowed)

    def test_deadline_zero_is_noop(self):
        with run_deadline(0):
            marker = True
        self.assertTrue(marker)

    def test_deadline_reaps_hung_child(self):
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        started = time.monotonic()
        raised = False
        try:
            with run_deadline(0.5):
                subprocess.run(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    check=False, timeout=25,
                )
        except RunDeadlineExceeded:
            raised = True
        elapsed = time.monotonic() - started
        self.assertTrue(raised, "deadline must fire while a child is hung")
        self.assertLess(elapsed, 10.0, "hung child must not outlive the deadline")


class NestedDeadlineTests(unittest.TestCase):
    def _spin(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            pass

    def test_inner_longer_alarm_cannot_mask_shorter_outer(self):
        fired = False
        started = time.monotonic()
        try:
            with run_deadline(0.3):
                with wallclock_alarm(30.0):
                    self._spin(3.0)
        except RunDeadlineExceeded:
            fired = True
        self.assertTrue(fired, "outer whole-run deadline must not be masked")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_nested_alarms_do_not_extend_the_total(self):
        fired = False
        started = time.monotonic()
        try:
            with run_deadline(0.5):
                for _ in range(5):
                    with wallclock_alarm(0.3):
                        time.sleep(0.2)
        except RunDeadlineExceeded:
            fired = True
        self.assertTrue(fired, "repeated nested timers must not postpone the total")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_source_broad_except_does_not_swallow_outer_deadline(self):
        swallowed = False
        fired = False
        try:
            with run_deadline(0.3):
                try:
                    with wallclock_alarm(30.0):
                        self._spin(2.0)
                except Exception:  # noqa: BLE001 - a source handler like this must not win
                    swallowed = True
        except RunDeadlineExceeded:
            fired = True
        self.assertTrue(fired)
        self.assertFalse(swallowed, "outer whole-run deadline must not be swallowed")

    def test_caught_inner_source_timeout_still_enforces_outer_deadline(self):
        # Negative control: removing the inner (earliest) deadline before raising
        # must NOT remove the outer whole-run bound. Catching SourceTimeout and
        # continuing to spin must still abort at the outer deadline.
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        events = []
        started = time.monotonic()
        try:
            with run_deadline(0.4):
                with wallclock_alarm(0.15):
                    try:
                        self._spin(2.0)
                    except SourceTimeout:
                        events.append("source")
                        self._spin(2.0)
        except RunDeadlineExceeded:
            events.append("run")
        self.assertEqual(events, ["source", "run"])
        self.assertLess(time.monotonic() - started, 2.0,
                        "outer run bound must still abort after the inner is caught")

    def test_removed_expired_deadline_fires_at_most_once(self):
        # Negative control for the removal-before-raise fix: once a deadline has
        # fired it is gone from the registry, so the same scope cannot re-arm it
        # into a second fire / signal storm.
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        events = []
        try:
            with run_deadline(0.2):
                try:
                    self._spin(0.5)
                except RunDeadlineExceeded:
                    events.append("first")
                    # The deadline was removed before raising; continuing to spin
                    # in this same scope must NOT fire it again.
                    self._spin(0.3)
        except RunDeadlineExceeded:
            events.append("escaped")
        self.assertEqual(events, ["first"], "an expired deadline must fire at most once")

    def test_inner_source_timeout_keeps_its_identity(self):
        caught = None
        with run_deadline(30.0):
            try:
                with wallclock_alarm(0.2):
                    time.sleep(1.0)
            except SourceTimeout:
                caught = "source"
        self.assertEqual(caught, "source", "an inner per-source expiry stays a SourceTimeout")

    def test_expired_outer_fires_on_restoration(self):
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        fired = False
        try:
            with run_deadline(0.3):
                with wallclock_alarm(30.0):
                    signal.setitimer(signal.ITIMER_REAL, 0)  # simulate lost delivery
                    time.sleep(0.5)  # outer expires while inner is still active
                time.sleep(0.1)
        except RunDeadlineExceeded:
            fired = True
        self.assertTrue(fired, "an outer deadline expired during an inner scope must fire on restore")

    def test_expired_outer_not_dropped_when_inner_exits_normally(self):
        # Deterministic negative control for the outer-deadline fallthrough: with
        # delivery lost, the inner scope exits WITHOUT a pending exception and the
        # outer bound has already expired. The outer must fire at inner teardown,
        # not be silently disarmed when the outer scope then exits microseconds
        # later. (The sibling test above only passes because of its trailing
        # ``time.sleep``; here there is no grace period.)
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        events = []
        started = time.monotonic()
        try:
            with run_deadline(0.2):
                with wallclock_alarm(30.0):
                    signal.setitimer(signal.ITIMER_REAL, 0)  # simulate lost delivery
                    self._spin(0.35)  # outer expires; inner exits normally
                events.append("inner-exited")
        except RunDeadlineExceeded:
            events.append("run")
        self.assertEqual(events, ["run"], "an expired outer must fire even with no grace period")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_expired_run_not_dropped_when_top_scope_exits_normally(self):
        # A signal deferred at a loop's unprotected backward edge, or a lost
        # timer delivery, must not disappear when the outermost scope tears down.
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        events = []
        try:
            with run_deadline(0.05):
                signal.setitimer(signal.ITIMER_REAL, 0)  # deterministic lost delivery
                time.sleep(0.1)
            events.append("escaped")
        except RunDeadlineExceeded:
            events.append("run")
        self.assertEqual(events, ["run"], "a due outer scope must fire on teardown")

    def test_signal_mask_restored_after_deadline(self):
        if not hasattr(signal, "pthread_sigmask"):
            self.skipTest("pthread_sigmask required")
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        with self.assertRaises(RunDeadlineExceeded):
            with run_deadline(0.05):
                time.sleep(1.0)
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), previous)

    def test_true_ambient_survives_deadline_removed_while_scope_active(self):
        # Deterministic negative control for ambient teardown reliability: a
        # deadline that fires is removed from the shared registry while its scope
        # is still on the stack, so a subsequent nested scope must NOT mistake
        # registry emptiness for "no scope active" and capture the dispatcher as
        # the ambient handler. The real ambient handler must still be restored.
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        previous = signal.signal(signal.SIGALRM, _guard)
        inside = None
        try:
            with run_deadline(0.05):
                try:
                    self._spin(0.2)
                except RunDeadlineExceeded:
                    # Fired deadline is already gone from the registry; the outer
                    # scope is still active, so the true ambient must be retained.
                    with wallclock_alarm(5.0):
                        self._spin(0.02)
            inside = signal.getsignal(signal.SIGALRM)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        self.assertEqual(inside, _guard,
                         "the real ambient handler must survive a re-entered scope")

    def test_nonfinite_deadline_is_rejected_loudly(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                with run_deadline(bad):
                    pass

    def test_wallclock_alarm_zero_is_still_bounded(self):
        caught = None
        started = time.monotonic()
        try:
            with wallclock_alarm(0):
                time.sleep(1.0)
        except SourceTimeout:
            caught = "source"
        self.assertEqual(caught, "source")
        self.assertLess(time.monotonic() - started, 1.0)

    def test_off_main_thread_is_safe_noop(self):
        result: dict = {}

        def worker() -> None:
            try:
                with run_deadline(0.1):
                    time.sleep(0.3)
                result["ok"] = True
            except BaseException as exc:  # noqa: BLE001 - record any escape for the assert
                result["error"] = repr(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertTrue(result.get("ok"), result)

    def test_nested_deadline_releases_shared_lock(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(CLOCK_ISO))
            raised = False
            with store.lock() as acquired:
                self.assertTrue(acquired)
                try:
                    with run_deadline(0.3):
                        with wallclock_alarm(30.0):
                            time.sleep(1.0)
                except RunDeadlineExceeded:
                    raised = True
            self.assertTrue(raised)
            with store.lock() as reacquired:
                self.assertTrue(reacquired, "shared lock must be free after a nested abort")

    def test_main_run_bounded_across_nested_source_alarm(self):
        import proactive_pilot.main as M

        def _blocking_sources(*args: object, **kwargs: object):
            def factory() -> SourceResult:
                with wallclock_alarm(30.0):
                    self._spin(3.0)
                return SourceResult("fake", "ok", checked_at="")
            return [factory]

        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            root = Path(tmp)
            policy = root / "policy.json"
            policy.write_text(json.dumps({"run_wallclock_seconds": 0.4}), encoding="utf-8")
            prefs = root / "prefs.json"
            prefs.write_text(json.dumps({
                "schema": 1, "version": 0, "explicit": [], "inferred": [],
                "snoozes": [], "paused": {"active": False},
            }), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            original = M.default_sources
            M.default_sources = _blocking_sources
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = M.main([
                        "--run", "--no-model",
                        "--state-dir", str(root / "state"),
                        "--policy", str(policy),
                        "--preferences", str(prefs),
                    ])
            finally:
                M.default_sources = original
        self.assertEqual(code, 1, "whole-run deadline must abort the fake hang")
        self.assertEqual(out.getvalue(), "", "an aborted run must not emit a deliverable")
        self.assertIn("wall-clock bound", err.getvalue())


class SigalrmDeadlineStressTests(unittest.TestCase):
    """Bounded real-SIGALRM stress with numeric success/error accounting.

    Each iteration must either raise the exact expected deadline exception or the
    iteration is recorded with a label. No external process, network, mock or
    fabricated success: this only schedules/clears the real ``ITIMER_REAL`` on
    the main thread. Everything is time-bounded so a regression can never spin
    for minutes.
    """

    def _spin(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            pass

    def test_repeated_outer_deadlines_all_fire(self):
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        iterations = 12
        successes = 0
        errors: list[str] = []
        started = time.monotonic()
        for _ in range(iterations):
            try:
                with run_deadline(0.01):
                    self._spin(0.3)
            except RunDeadlineExceeded:
                successes += 1
            except BaseException as exc:  # noqa: BLE001 - record the wrong-class escape
                errors.append(repr(exc))
            else:
                errors.append("no-expiry")
        self.assertEqual(errors, [], f"numeric stress errors: {errors}")
        self.assertEqual(successes, iterations)
        self.assertLess(time.monotonic() - started, 8.0)

    def test_repeated_nested_timeouts_keep_outer_bound(self):
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        iterations = 10
        successes = 0
        errors: list[str] = []
        started = time.monotonic()
        for _ in range(iterations):
            events: list[str] = []
            try:
                # Per-source bounds are floored at 0.05 s, so keep the outer
                # whole-run bound comfortably later than the inner one.
                with run_deadline(0.12):
                    with wallclock_alarm(0.05):
                        try:
                            self._spin(0.3)
                        except SourceTimeout:
                            events.append("source")
                            self._spin(0.3)
            except RunDeadlineExceeded:
                events.append("run")
            except BaseException as exc:  # noqa: BLE001 - record the wrong-class escape
                errors.append(repr(exc))
                continue
            if events == ["source", "run"]:
                successes += 1
            else:
                errors.append(str(events))
        self.assertEqual(errors, [], f"numeric nested stress errors: {errors}")
        self.assertEqual(successes, iterations)
        self.assertLess(time.monotonic() - started, 12.0)

    def test_repeated_ambient_restore_leaves_no_timer(self):
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        previous = signal.getsignal(signal.SIGALRM)
        caught = 0
        for _ in range(12):
            try:
                with run_deadline(0.01):
                    self._spin(0.05)
            except RunDeadlineExceeded:
                caught += 1
        self.assertEqual(caught, 12)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0),
                         "no armed timer may be left behind after the outermost scope")
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous,
                         "ambient handler must be restored after repeated scopes")


class ReminderBusyStatusTests(unittest.TestCase):
    def _prefs(self, root: Path) -> Path:
        prefs = root / "prefs.json"
        prefs.write_text(json.dumps({
            "schema": 1, "version": 0, "explicit": [], "inferred": [],
            "snoozes": [], "paused": {"active": False},
        }), encoding="utf-8")
        return prefs

    def test_busy_dispatch_is_silent_skip_not_failure_spam(self):
        from proactive_pilot import reminders as R

        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            prefs = self._prefs(root)
            store = StateStore(state_dir, R.Clock())
            out, err = io.StringIO(), io.StringIO()
            with store.lock() as acquired:
                self.assertTrue(acquired)
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = R.main([
                        "--state-dir", str(state_dir),
                        "--preferences", str(prefs),
                        "dispatch",
                    ])
            self.assertEqual(out.getvalue(), "",
                             "busy skip must not write a deliverable to stdout")
            self.assertEqual(code, R.DISPATCH_BUSY_EXIT,
                             "scheduled dispatch overlap must stay a silent exit-0 skip")
            self.assertEqual(code, R.EXIT_OK)
            self.assertIn("skip-lock", err.getvalue())

    def test_busy_add_is_distinct_from_success(self):
        from proactive_pilot import reminders as R

        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            prefs = self._prefs(root)
            store = StateStore(state_dir, R.Clock())
            out, err = io.StringIO(), io.StringIO()
            with store.lock() as acquired:
                self.assertTrue(acquired)
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = R.main([
                        "--state-dir", str(state_dir),
                        "--preferences", str(prefs),
                        "--add-time", "--text", "synthetic", "--provenance", "user request",
                        "--due", "2026-10-03T12:00:00+08:00",
                    ])
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(code, R.EXIT_BUSY)
            self.assertIn("skip-lock", err.getvalue())


class SignalDeliveryRegressionTests(unittest.TestCase):
    """Regression controls for deadline delivery to the main thread.

    These tests pin the guarantees the standard watchdog provides: a deadline
    aborts a hung tight loop and reaches its lexical ``except``, interrupts a
    blocked call, and releases the advisory lock with the registry/depth/timer
    left clean afterwards. Delivery is the standard-library synchronous
    ``SIGALRM`` handler raise; these controls do not assume any
    interpreter-specific exception routing.
    """

    def test_repeated_tight_loop_deadlines_always_abort_the_run(self):
        # Run in fresh child processes so an uncatchable delivery cannot be
        # absorbed by the test frame: the process must terminate before the
        # (much longer) loop would have finished, proving the deadline aborted it.
        import subprocess

        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        script = (
            "import sys, time\n"
            "from proactive_pilot.watchdog import run_deadline, RunDeadlineExceeded\n"
            "try:\n"
            "    with run_deadline(0.2):\n"
            "        end = time.monotonic() + 30.0\n"
            "        while time.monotonic() < end:\n"
            "            pass\n"
            "except RunDeadlineExceeded:\n"
            "    print('CAUGHT')\n"
            "    sys.exit(0)\n"
            "print('FINISHED')\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        for _ in range(8):
            started = time.monotonic()
            proc = subprocess.run([sys.executable, "-c", script],
                                  capture_output=True, text=True, env=env, timeout=20)
            elapsed = time.monotonic() - started
            self.assertEqual(proc.returncode, 0,
                             f"the deadline must be caught by the intended handler: {proc.stderr}")
            self.assertEqual(proc.stdout.strip(), "CAUGHT",
                             "the exact RunDeadlineExceeded must reach its lexical except")
            self.assertNotIn("FINISHED", proc.stdout,
                             "a hung tight loop must never run to completion")
            self.assertLess(elapsed, 10.0, "the deadline must abort the hung loop promptly")

    def test_registry_depth_and_lock_clean_after_many_deliveries(self):
        # Cooperative (interruptible) waits: the deadline lands at an explicit
        # bytecode boundary, so the exception is catchable and the registry,
        # depth counter and advisory lock can be asserted clean afterwards.
        import proactive_pilot.watchdog as W
        from proactive_pilot.clock import SeededClock
        from proactive_pilot.state import StateStore

        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            store = StateStore(Path(tmp), SeededClock(CLOCK_ISO))
            for _ in range(100):
                with store.lock() as acquired:
                    self.assertTrue(acquired)
                    with self.assertRaises(RunDeadlineExceeded):
                        with run_deadline(0.05):
                            time.sleep(5.0)
                with store.lock() as reacquired:
                    self.assertTrue(reacquired, "lock must be released after every deadline")
        self.assertEqual(W._DEPTH, 0)
        self.assertEqual(W._REGISTRY, [])
        if hasattr(signal, "getitimer"):
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_deadline_interrupts_a_blocked_call(self):
        if not hasattr(signal, "SIGALRM"):
            self.skipTest("SIGALRM required")
        started = time.monotonic()
        with self.assertRaises(RunDeadlineExceeded):
            with run_deadline(0.3):
                time.sleep(30)
        self.assertLess(time.monotonic() - started, 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
