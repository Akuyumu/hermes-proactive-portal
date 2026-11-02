"""Synthetic tests for the bounded agentic task consumer.

No process, model, network, credential or live state is used. A fake runner and
the real temporary StateStore/preparation artifact path exercise the seams.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import agentic_task  # noqa: E402
from proactive_pilot import preparation as prep  # noqa: E402
from proactive_pilot import responsibilities as R  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.native_task_run import NativeTaskResult  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

T0 = datetime.fromisoformat("2026-10-20T10:00:00+08:00")


class FakeRunner:
    def __init__(self, result, on_run=None, before_dispatch_action=None):
        self.result = result
        self.on_run = on_run
        self.before_dispatch_action = before_dispatch_action
        self.calls = []
        self.dispatched = False

    def run(self, instructions, *, toolsets, expected_tools, policy,
            before_dispatch=None):
        self.calls.append({
            "instructions": instructions,
            "toolsets": toolsets,
            "expected_tools": expected_tools,
            "policy": policy,
        })
        if self.before_dispatch_action is not None:
            self.before_dispatch_action()
        if before_dispatch is not None and before_dispatch() is not True:
            return NativeTaskResult(
                status="denied", reason="before_dispatch denied dispatch")
        self.dispatched = True
        if self.on_run is not None:
            self.on_run()
        return self.result


class RaisingRunner:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def run(self, instructions, **kwargs):
        self.calls += 1
        raise self.exc


class AgenticTaskTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "var", SeededClock(T0.isoformat()))
        self.artifacts = self.root / "artifacts"
        patcher = mock.patch.object(
            prep, "default_artifact_root", lambda: self.artifacts)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rec = R.create(
            self.store,
            desired_outcome="Prepare a bounded checklist",
            acceptance_criterion="The checklist cites the supplied conversation",
            provenance={
                "kind": "user_commitment",
                "source_ref": "conversation:1",
                "conversation_ref": "chat",
                "at": T0.isoformat(),
            },
            due_at=(T0 - timedelta(minutes=1)).isoformat(),
            evidence_refs=["conversation:1"],
            facts=[{
                "ref": "conversation:1",
                "text": "Requirement A",
                "provenance": "user conversation",
                "observed_at": T0.isoformat(),
            }],
            now=T0,
        )

    def _claim(self):
        claim = R.claim_due(self.store, now=T0)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["id"], self.rec["id"])
        return claim

    def _run(self, claim, runner, gate=lambda: True, **overrides):
        kwargs = {
            "claim_token": claim["claim_token"],
            "runner": runner,
            "gate_fn": gate,
            "toolsets": ["secretary_background"],
            "expected_tools": ["secretary_task_inspect"],
            "max_turns": 3,
            "now": T0,
        }
        kwargs.update(overrides)
        return agentic_task.run_agentic_task(
            self.store, self.rec["id"], **kwargs)

    def _write_artifact(self, claim):
        return prep.write_artifact(
            self.store,
            responsibility_id=self.rec["id"],
            claim_token=claim["claim_token"],
            generation=claim["generation"],
            source_version=claim["source_version"],
            title="Bounded draft",
            content="Draft [[ref: conversation:1]]",
            evidence_refs=["conversation:1"],
            fmt="text",
            gate_fn=lambda: True,
            now=T0,
        )

    def _snapshot(self):
        return R.export_records(self.store)

    def _set_clock(self, when):
        self.store.clock._now = when

    def test_happy_path_executed_and_verified_is_recorded_success(self):
        claim = self._claim()
        runner = FakeRunner(
            NativeTaskResult(
                status="executed_unverified",
                exit_code=0,
                diagnostics=("exit=0", "stderr_chars=0"),
            ),
            on_run=lambda: self._write_artifact(claim),
        )

        outcome = self._run(claim, runner)

        self.assertEqual(outcome.status, "success")
        self.assertTrue(outcome.verified)
        self.assertTrue(outcome.recorded)
        self.assertEqual(outcome.runner_status, "executed_unverified")
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(runner.dispatched)
        call = runner.calls[0]
        self.assertEqual(call["toolsets"], ["secretary_background"])
        self.assertEqual(call["expected_tools"], ["secretary_task_inspect"])
        self.assertEqual(call["policy"]["evaluator"]["max_turns"], 3)
        self.assertNotIn(claim["claim_token"], call["instructions"])
        record = R.inspect(self.store, self.rec["id"], include_claim=True)
        self.assertEqual(record["ready"]["status"], "queued")
        self.assertTrue(record["ready"]["artifact_ref"])
        self.assertEqual(outcome.artifact_hash, record["ready"]["hash"])
        self.assertIn("verified", record["checkpoint"]["summary"])
        self.assertTrue(outcome.artifact_ref.startswith("artifact:"))
        self.assertTrue(outcome.artifact_hash)

    def test_executed_unverified_with_failed_verification_is_non_success(self):
        claim = self._claim()
        runner = FakeRunner(
            NativeTaskResult(status="executed_unverified", exit_code=0))

        outcome = self._run(claim, runner)

        self.assertEqual(outcome.status, "unverified")
        self.assertFalse(outcome.verified)
        self.assertTrue(outcome.recorded)
        self.assertEqual(len(runner.calls), 1)
        record = R.inspect(self.store, self.rec["id"], include_claim=True)
        self.assertEqual(record["ready"]["status"], "")
        self.assertEqual(record["status"], R.ACCEPTED)
        self.assertIn("non-success", record["checkpoint"]["summary"])

    def test_expired_claim_is_refused_before_runner(self):
        claim = self._claim()
        runner = FakeRunner(NativeTaskResult(status="executed_unverified"))
        before = self._snapshot()

        with self.assertRaises(agentic_task.AgenticTaskRefused):
            self._run(claim, runner, now=T0 + timedelta(minutes=30))

        self.assertEqual(runner.calls, [])
        self.assertEqual(self._snapshot(), before)

    def test_wrong_generation_claim_is_refused_before_runner(self):
        claim = self._claim()
        R.update(
            self.store, self.rec["id"],
            desired_outcome="Changed outcome", now=T0)
        before = self._snapshot()
        runner = FakeRunner(NativeTaskResult(status="executed_unverified"))

        with self.assertRaises(agentic_task.AgenticTaskRefused):
            self._run(claim, runner)

        self.assertEqual(runner.calls, [])
        self.assertEqual(self._snapshot(), before)

    def test_paused_or_denied_gate_is_refused_before_runner(self):
        claim = self._claim()
        before = self._snapshot()
        runner = FakeRunner(NativeTaskResult(status="executed_unverified"))

        with self.assertRaises(agentic_task.AgenticTaskRefused):
            self._run(claim, runner, gate=lambda: (False, "paused"))

        self.assertEqual(runner.calls, [])
        self.assertEqual(self._snapshot(), before)

    def test_missing_gate_is_refused_before_runner(self):
        claim = self._claim()
        runner = FakeRunner(NativeTaskResult(status="executed_unverified"))

        with self.assertRaises(agentic_task.AgenticTaskRefused):
            self._run(claim, runner, gate=None)

        self.assertEqual(runner.calls, [])

    def test_runner_denial_is_surfaced_honestly(self):
        claim = self._claim()
        runner = FakeRunner(NativeTaskResult(
            status="denied", reason="before_dispatch denied dispatch"))

        outcome = self._run(claim, runner)

        self.assertEqual(outcome.status, "denied")
        self.assertFalse(outcome.verified)
        self.assertTrue(outcome.recorded)
        self.assertEqual(outcome.runner_status, "denied")
        self.assertIn("denied", outcome.reason)
        self.assertEqual(len(runner.calls), 1)
        record = R.inspect(self.store, self.rec["id"], include_claim=True)
        self.assertIn("non-success", record["checkpoint"]["summary"])

    def test_runner_timeout_is_surfaced_honestly(self):
        claim = self._claim()
        runner = FakeRunner(NativeTaskResult(
            status="timeout", reason="native CLI exceeded its deadline",
            diagnostics=("timeout_seconds=100",)))

        outcome = self._run(claim, runner)

        self.assertEqual(outcome.status, "timeout")
        self.assertFalse(outcome.verified)
        self.assertTrue(outcome.recorded)
        self.assertEqual(outcome.runner_status, "timeout")
        self.assertEqual(len(runner.calls), 1)

    def test_oversized_instructions_are_refused_before_runner(self):
        claim = self._claim()
        runner = FakeRunner(NativeTaskResult(status="executed_unverified"))
        before = self._snapshot()

        with mock.patch.object(agentic_task, "MAX_INSTRUCTION_CHARS", 1):
            with self.assertRaises(agentic_task.AgenticTaskRefused):
                self._run(claim, runner)

        self.assertEqual(runner.calls, [])
        self.assertEqual(self._snapshot(), before)

    def test_runner_exception_is_not_retried_and_leaks_nothing(self):
        claim = self._claim()
        runner = RaisingRunner(RuntimeError("private /home/owner/secret"))

        outcome = self._run(claim, runner)

        self.assertEqual(outcome.status, "error")
        self.assertFalse(outcome.verified)
        self.assertTrue(outcome.recorded)
        self.assertEqual(runner.calls, 1)
        self.assertNotIn("private", repr(outcome))
        self.assertNotIn("/home/owner", repr(outcome))

    def test_update_after_entry_before_dispatch_denies_and_does_not_dispatch(self):
        claim = self._claim()
        runner = FakeRunner(
            NativeTaskResult(status="executed_unverified", exit_code=0),
            before_dispatch_action=lambda: R.update(
                self.store, self.rec["id"],
                desired_outcome="Changed after entry", now=T0),
        )

        outcome = self._run(claim, runner)

        self.assertEqual(outcome.status, "denied")
        self.assertFalse(outcome.verified)
        self.assertFalse(outcome.recorded)
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(runner.dispatched)
        record = R.inspect(self.store, self.rec["id"], include_claim=True)
        self.assertEqual(record["status"], R.ACCEPTED)
        self.assertEqual(record["ready"]["status"], "")

    def test_claim_expiry_before_dispatch_denies_with_fixed_initial_now(self):
        claim = self._claim()
        expires = datetime.fromisoformat(claim["expires_at"])
        runner = FakeRunner(
            NativeTaskResult(status="executed_unverified", exit_code=0),
            before_dispatch_action=lambda: self._set_clock(
                expires + timedelta(seconds=1)),
        )

        outcome = self._run(claim, runner)

        self.assertEqual(outcome.status, "denied")
        self.assertFalse(outcome.verified)
        self.assertFalse(outcome.recorded)
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(runner.dispatched)

    def test_gate_revoked_after_initial_admission_denies_without_dispatch(self):
        claim = self._claim()
        gate_state = {"allowed": True}

        def gate():
            return gate_state["allowed"]

        runner = FakeRunner(
            NativeTaskResult(status="executed_unverified", exit_code=0),
            before_dispatch_action=lambda: gate_state.__setitem__(
                "allowed", False),
        )

        outcome = self._run(claim, runner, gate=gate)

        self.assertEqual(outcome.status, "denied")
        self.assertFalse(outcome.verified)
        self.assertTrue(outcome.recorded)
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(runner.dispatched)
        record = R.inspect(self.store, self.rec["id"], include_claim=True)
        self.assertIn("non-success", record["checkpoint"]["summary"])

    def test_source_version_change_after_entry_denies_without_dispatch(self):
        claim = self._claim()
        runner = FakeRunner(
            NativeTaskResult(status="executed_unverified", exit_code=0),
            before_dispatch_action=lambda: R.update(
                self.store, self.rec["id"],
                source_version="conversation:2", now=T0),
        )

        outcome = self._run(claim, runner)

        self.assertEqual(outcome.status, "denied")
        self.assertFalse(outcome.verified)
        self.assertFalse(outcome.recorded)
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(runner.dispatched)
        record = R.inspect(self.store, self.rec["id"], include_claim=True)
        self.assertEqual(record["source_version"], "conversation:2")

    def test_claim_expiry_after_child_artifact_cannot_record_success(self):
        claim = self._claim()
        expires = datetime.fromisoformat(claim["expires_at"])

        def write_then_expire():
            self._write_artifact(claim)
            self._set_clock(expires + timedelta(seconds=1))

        runner = FakeRunner(
            NativeTaskResult(status="executed_unverified", exit_code=0),
            on_run=write_then_expire,
        )

        outcome = self._run(claim, runner)

        self.assertEqual(outcome.status, "unverified")
        self.assertFalse(outcome.verified)
        self.assertFalse(outcome.recorded)
        self.assertEqual(outcome.runner_status, "executed_unverified")
        self.assertTrue(runner.dispatched)
        record = R.inspect(self.store, self.rec["id"], include_claim=True)
        self.assertEqual(record["status"], R.ACCEPTED)
        self.assertEqual(record["ready"]["status"], "queued")
        self.assertNotIn("agentic task verified", record["checkpoint"]["summary"])

    def test_secrets_and_claim_token_never_appear_in_outcome_or_summary(self):
        claim = self._claim()
        token = claim["claim_token"]
        secret = "sk-abcdefghijklmnopqrst"
        runner = FakeRunner(
            NativeTaskResult(
                status="executed_unverified",
                exit_code=0,
                output=f"hidden output {secret}",
                diagnostics=(f"note={token}", f"api_key={secret}"),
            ),
            on_run=lambda: self._write_artifact(claim),
        )

        outcome = self._run(claim, runner)

        rendered = repr(outcome)
        self.assertNotIn(token, rendered)
        self.assertNotIn(secret, rendered)
        record = R.inspect(self.store, self.rec["id"], include_claim=True)
        summary = record["checkpoint"]["summary"]
        self.assertNotIn(token, summary)
        self.assertNotIn(secret, summary)
        self.assertNotIn("hidden output", summary)
        self.assertTrue(outcome.verified)
        self.assertTrue(outcome.recorded)


if __name__ == "__main__":
    unittest.main()
