"""Runs the production-module reminder E2E verification harness.

Fake event transport + explicit fake clock + isolated owner-only state.
No send, no network, no private/default client, no real reminder writes.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.reminder_verify import (  # noqa: E402
    FakeEventTransport,
    ReminderVerificationHarness,
    parent_evidence_plan,
)

SG = ZoneInfo("Asia/Singapore")
START = datetime(2026, 10, 3, 12, 0, tzinfo=SG)


class ReminderVerificationHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=str(ROOT))
        self.root = Path(self._tmp.name) / "state"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_full_scenario_passes_all_checks_and_never_sends(self):
        getter = FakeEventTransport()
        harness = ReminderVerificationHarness(self.root, start=START, getter=getter)
        evidence = harness.run()
        self.assertTrue(evidence["ok"], json.dumps(evidence["checks"], indent=2))
        self.assertEqual(evidence["checks_passed"], evidence["checks_total"])
        self.assertFalse(evidence["sends"])
        self.assertFalse(evidence["private_clients"])
        # Every check name is present and passed.
        names = {c["check"] for c in evidence["checks"]}
        for required in (
            "add_pending", "add_exact_trigger", "dedup_same_id", "restart_single_pending",
            "dispatch_emits_once", "dispatch_emitted", "dispatch_no_duplicate",
            "move_earlier_emits", "cancel_no_emission", "queue_bounded",
            "queue_overflow_pending", "quiet_hours_bypass", "pause_silent",
            "pause_zero_reads", "negative_missing_provenance_rejected",
            "negative_malformed_due_rejected", "negative_source_failure_uncertain",
        ):
            self.assertIn(required, names)

    def test_state_root_is_owner_only(self):
        Harness = ReminderVerificationHarness
        Harness(self.root, start=START, getter=FakeEventTransport()).run()
        mode = stat.S_IMODE(os.stat(self.root).st_mode)
        self.assertEqual(mode, 0o700)

    def test_harness_is_isolated_per_instance(self):
        first = ReminderVerificationHarness(self.root, start=START,
                                            getter=FakeEventTransport())
        first.run()
        other_root = Path(self._tmp.name) / "state2"
        other = ReminderVerificationHarness(other_root, start=START,
                                            getter=FakeEventTransport())
        self.assertTrue(other.run()["ok"])
        self.assertNotEqual(first.root, other.root)

    def test_parent_evidence_plan_is_read_only_and_distinguishes_routes(self):
        plan = parent_evidence_plan(state_dir=str(self.root))
        self.assertIn("parent-only", plan["scope"])
        self.assertIn("native scheduler", plan["route_a_standalone_native_one_shot"]["what_it_is"])
        self.assertIn("reminders.json", plan["route_b_durable_dispatcher"]["what_it_is"])
        self.assertTrue(plan["distinguishing_checks"])
        self.assertTrue(plan["route_b_durable_dispatcher"]["where_to_look"])
        self.assertIn("no Telegram/network send from this harness", plan["hard_stops"])


if __name__ == "__main__":
    unittest.main()
