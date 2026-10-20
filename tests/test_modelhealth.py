"""Model-outage lifecycle: one atomic model-free notice, retries, unknown, recovery."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import modelhealth as M  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

T0 = datetime.fromisoformat("2026-10-20T10:00:00+08:00")


class ModelHealthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            dir=os.environ.get("TMPDIR") or tempfile.gettempdir())
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state", SeededClock(T0.isoformat()))
        self.addCleanup(self._tmp.cleanup)

    def both_fail(self, now=T0, retry_after=None):
        M.record_attempt(self.store, role="primary", outcome="failure",
                         failure_kind="timeout", now=now,
                         base_backoff_seconds=60, max_backoff_seconds=3600)
        return M.record_attempt(self.store, role="fallback", outcome="failure",
                                failure_kind="rate_limit", retry_after=retry_after,
                                now=now, base_backoff_seconds=60, max_backoff_seconds=3600)

    def claim(self, now=T0):
        return M.claim_notice(self.store, now=now, attention_fn=lambda: (True, "ok"))

    def test_primary_only_does_not_arm_notice(self):
        M.record_attempt(self.store, role="primary", outcome="failure",
                         failure_kind="timeout", now=T0)
        decision = self.claim()
        self.assertFalse(decision["eligible"])
        self.assertEqual(M.status(self.store)["notice"]["status"], "")

    def test_primary_fallback_then_one_fixed_notice_across_restarts(self):
        self.both_fail()
        st = M.status(self.store)
        self.assertEqual(st["notice"]["status"], "pending")
        self.assertEqual(st["notice"]["text"], M.NOTICE_TEXT)
        self.assertEqual([d["kind"] for d in st["incident"]["diagnostics"]],
                         ["timeout", "rate_limit"])
        first = self.claim()
        self.assertTrue(first["eligible"])
        self.assertEqual(first["text"], M.NOTICE_TEXT)
        self.assertTrue(first["notice_token"])
        self.assertNotIn("token", M.status(self.store)["notice"])
        # Repeated failure and a fresh process/state load do not create another.
        self.both_fail(now=T0 + timedelta(minutes=1))
        fresh = StateStore(self.root / "state",
                           SeededClock((T0 + timedelta(minutes=2)).isoformat()))
        second = M.claim_notice(fresh, now=T0 + timedelta(minutes=2),
                                attention_fn=lambda: True)
        self.assertFalse(second["eligible"])  # already claimed by the winner
        M.mark_notice_delivered(self.store, incident_id=first["incident_id"],
                                notice_token=first["notice_token"], now=T0)
        self.assertFalse(self.claim(now=T0 + timedelta(minutes=3))["eligible"])

    def test_blocked_attention_retains_exactly_one_pending(self):
        self.both_fail()
        blocked = M.claim_notice(self.store, now=T0,
                                 attention_fn=lambda: (False, "quiet hours"))
        self.assertFalse(blocked["eligible"])
        self.assertEqual(blocked["reason"], "quiet hours")
        self.assertEqual(M.status(self.store)["notice"]["status"], "pending")
        no_gate = M.claim_notice(self.store, now=T0)
        self.assertFalse(no_gate["eligible"])
        self.assertEqual(no_gate["reason"], "attention gate required")
        self.assertEqual(M.status(self.store)["notice"]["status"], "pending")
        allowed = M.claim_notice(self.store, now=T0 + timedelta(minutes=5),
                                 attention_fn=lambda: (True, "ok"))
        self.assertTrue(allowed["eligible"])
        self.assertEqual(allowed["text"], M.NOTICE_TEXT)

    def test_existing_cap_gate_block_retains_single_pending(self):
        from proactive_pilot.gates import notice_allowed

        self.both_fail()
        gate_state = {"delivered": [], "items": {}}
        policy = {"daily_normal_cap": 0, "cooldown_minutes": 0,
                  "quiet_start_hour": 0, "quiet_end_hour": 8,
                  "timezone": "Asia/Singapore"}
        prefs = {"paused": {"active": False}, "snoozes": []}
        blocked = M.claim_notice(
            self.store, now=T0,
            attention_fn=lambda: notice_allowed(gate_state, policy, prefs, T0))
        self.assertFalse(blocked["eligible"])
        self.assertEqual(blocked["reason"], "daily cap")
        self.assertEqual(M.status(self.store)["notice"]["status"], "pending")
        policy["daily_normal_cap"] = 1
        allowed = M.claim_notice(
            self.store, now=T0,
            attention_fn=lambda: notice_allowed(gate_state, policy, prefs, T0))
        self.assertTrue(allowed["eligible"])

    def test_concurrent_threads_only_one_notice_winner(self):
        self.both_fail()
        results = []
        barrier = threading.Barrier(6)

        def worker():
            barrier.wait()
            try:
                results.append(M.claim_notice(
                    self.store, now=T0, attention_fn=lambda: True))
            except M.ModelHealthError:
                results.append({"eligible": False, "reason": "busy"})

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [row for row in results if row.get("eligible")]
        self.assertEqual(len(winners), 1, results)

    def test_concurrent_processes_only_one_notice_winner(self):
        self.both_fail()
        code = (
            "import json,sys\n"
            "from datetime import datetime\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            "from proactive_pilot import modelhealth as M\n"
            "from proactive_pilot.clock import SeededClock\n"
            "from proactive_pilot.state import StateStore\n"
            f"store=StateStore({str(self.root / 'state')!r}, SeededClock({T0.isoformat()!r}))\n"
            "try:\n"
            f"    out=M.claim_notice(store, now=datetime.fromisoformat({T0.isoformat()!r}),"
            " attention_fn=lambda: True)\n"
            "except Exception as exc:\n"
            "    out={'eligible': False, 'reason': type(exc).__name__}\n"
            "print(json.dumps(out))\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
                 for _ in range(5)]
        rows = []
        for proc in procs:
            out, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, err)
            rows.append(json.loads(out.strip().splitlines()[-1]))
        winners = [row for row in rows if row.get("eligible")]
        self.assertEqual(len(winners), 1, rows)

    def test_unknown_never_retried_or_claimed_delivered(self):
        self.both_fail()
        claim = self.claim()
        M.mark_notice_unknown(self.store, incident_id=claim["incident_id"],
                              notice_token=claim["notice_token"], now=T0)
        self.assertEqual(M.status(self.store)["notice"]["status"], "unknown")
        self.assertFalse(self.claim(now=T0 + timedelta(hours=1))["eligible"])
        with self.assertRaises(M.ModelHealthError):
            M.mark_notice_delivered(self.store, incident_id=claim["incident_id"],
                                    notice_token=claim["notice_token"],
                                    now=T0 + timedelta(hours=2))
        self.assertEqual(M.status(self.store)["notice"]["status"], "unknown")

    def test_pending_cannot_become_delivered_without_attempt(self):
        self.both_fail()
        with self.assertRaises(M.ModelHealthError):
            M.mark_notice_delivered(self.store, incident_id="any", notice_token="any", now=T0)
        self.assertEqual(M.status(self.store)["notice"]["status"], "pending")

    def test_stale_receipt_cannot_mark_new_incident(self):
        self.both_fail()
        old = self.claim()
        M.mark_notice_unknown(self.store, incident_id=old["incident_id"],
                              notice_token=old["notice_token"], now=T0)
        M.recovery(self.store, role="fallback", now=T0 + timedelta(minutes=1))
        later = T0 + timedelta(hours=3)
        self.both_fail(now=later)
        new = M.claim_notice(self.store, now=later, attention_fn=lambda: True)
        self.assertTrue(new["eligible"])
        with self.assertRaises(M.ModelHealthError):
            M.mark_notice_delivered(self.store, incident_id=old["incident_id"],
                                    notice_token=old["notice_token"], now=later)
        with self.assertRaises(M.ModelHealthError):
            M.mark_notice_delivered(self.store, incident_id=new["incident_id"],
                                    notice_token=old["notice_token"], now=later)
        self.assertEqual(M.status(self.store)["notice"]["status"], "attempted")

    def test_recovery_rearms_a_new_later_incident(self):
        self.both_fail()
        first_id = M.status(self.store)["incident"]["id"]
        first = self.claim()
        M.mark_notice_delivered(self.store, incident_id=first["incident_id"],
                                notice_token=first["notice_token"], now=T0)
        M.recovery(self.store, role="fallback", now=T0 + timedelta(minutes=1))
        self.assertEqual(M.status(self.store)["incident"]["status"], "recovered")
        later = T0 + timedelta(hours=3)
        self.both_fail(now=later)
        st = M.status(self.store)
        self.assertNotEqual(st["incident"]["id"], first_id)
        self.assertEqual(st["notice"]["status"], "pending")
        self.assertTrue(M.claim_notice(self.store, now=later,
                                       attention_fn=lambda: True)["eligible"])

    def test_backoff_and_retry_after(self):
        M.record_attempt(self.store, role="primary", outcome="failure",
                         failure_kind="timeout", now=T0,
                         base_backoff_seconds=60, max_backoff_seconds=3600)
        st = M.status(self.store)
        self.assertEqual(st["retry_after"], (T0 + timedelta(seconds=60)).isoformat())
        self.assertFalse(M.probe_allowed(self.store, now=T0)["allowed"])
        self.assertTrue(M.probe_allowed(self.store, now=T0 + timedelta(seconds=61))["allowed"])

        other = StateStore(self.root / "state2", SeededClock(T0.isoformat()))
        M.record_attempt(other, role="primary", outcome="failure",
                         failure_kind="rate_limit", retry_after=600,
                         now=T0, base_backoff_seconds=60, max_backoff_seconds=3600)
        self.assertEqual(M.status(other)["retry_after"],
                         (T0 + timedelta(seconds=600)).isoformat())

    def test_retry_after_inf_overflow_and_http_date_are_clamped(self):
        for value in (float("inf"), "inf", "1e999", 10 ** 30):
            store = StateStore(self.root / f"state_{type(value).__name__}_{str(value)[:6]}",
                               SeededClock(T0.isoformat()))
            M.record_attempt(store, role="primary", outcome="failure",
                             failure_kind="rate_limit", retry_after=value, now=T0,
                             base_backoff_seconds=60, max_backoff_seconds=3600)
            retry = datetime.fromisoformat(M.status(store)["retry_after"])
            self.assertLessEqual(retry, T0 + timedelta(seconds=M.MAX_BACKOFF_SECONDS))
            self.assertGreaterEqual(retry, T0)
        store = StateStore(self.root / "state_http", SeededClock(T0.isoformat()))
        M.record_attempt(store, role="primary", outcome="failure",
                         failure_kind="rate_limit",
                         retry_after="Wed, 21 Oct 2037 07:28:00 GMT", now=T0,
                         base_backoff_seconds=60, max_backoff_seconds=3600)
        retry = datetime.fromisoformat(M.status(store)["retry_after"])
        self.assertLessEqual(retry, T0 + timedelta(seconds=M.MAX_BACKOFF_SECONDS))

    def test_config_change_probe_does_not_duplicate_notice(self):
        self.both_fail()
        before = M.status(self.store)["incident"]["id"]
        self.assertTrue(M.probe_allowed(self.store, now=T0,
                                        config_signature="cfg-v2")["allowed"])
        st = M.status(self.store)
        self.assertEqual(st["incident"]["id"], before)
        self.assertEqual(st["notice"]["status"], "pending")
        self.assertTrue(self.claim()["eligible"])

    def test_distinct_diagnostics_are_sanitized(self):
        for kind, detail in (("auth", "401 raw-secret-value"), ("config", "missing key sk-live"),
                             ("timeout", "provider said too slow"), ("rate_limit", "429")):
            M.record_attempt(self.store, role="primary", outcome="failure",
                             failure_kind=kind, detail=detail, now=T0)
            diag = M.status(self.store)["incident"]["diagnostics"][-1]
            self.assertEqual(diag["kind"], kind)
            self.assertNotIn("raw-secret", str(diag))
            self.assertNotIn("sk-live", str(diag))
        self.assertEqual(M.sanitize_failure_kind("nonsense", "401 unauthorized"), "auth")

    def test_invalid_state_fails_closed(self):
        self.store.save(M.STORE, {"schema": 99})
        with self.assertRaises(Exception):
            M.status(self.store)


if __name__ == "__main__":
    unittest.main()
