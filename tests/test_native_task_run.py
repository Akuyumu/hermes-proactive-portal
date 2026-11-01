"""Product unit tests using controlled low-level doubles only."""
from __future__ import annotations

import signal
import subprocess
import unittest

from proactive_pilot.native_task_run import (
    MAX_INSTRUCTIONS_CHARS,
    MAX_OUTPUT_CHARS,
    NativeTaskRunner,
)


class FakeProcess:
    def __init__(self, *, stdout="", stderr="", returncode=0, timeout_calls=0):
        self.pid = 4321
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self._timeout_calls = timeout_calls
        self.communicate_calls = []

    def communicate(self, input=None, timeout=None):
        self.communicate_calls.append((input, timeout))
        if self._timeout_calls > 0:
            self._timeout_calls -= 1
            raise subprocess.TimeoutExpired(cmd="hermes", timeout=timeout)
        return self.stdout, self.stderr


class FakePopen:
    def __init__(self, process, events):
        self.process = process
        self.events = events

    def __call__(self, argv, **kwargs):
        self.events.append("spawn")
        self.argv = argv
        self.kwargs = kwargs
        return self.process


def policy(**overrides):
    cfg = {
        "max_turns": 3,
        "run_budget_seconds": 20,
        "timeout_seconds": 35,
        "resolve_timeout_seconds": 5,
    }
    cfg.update(overrides)
    return {"evaluator": cfg}


class NativeTaskRunnerTests(unittest.TestCase):
    def runner(self, resolver, popen=None, killpg=None, events=None):
        events = events if events is not None else []
        fake_popen = FakePopen(popen or FakeProcess(), events)
        return NativeTaskRunner(
            resolver=resolver,
            popen_factory=fake_popen,
            killpg=killpg or (lambda pid, sig: None),
            clock=lambda: 100.0,
        ), fake_popen

    def test_zero_tool_success_is_executed_unverified_and_ordered(self):
        events = []
        calls = []

        def resolver(name, *, hermes_bin, timeout):
            events.append("resolve")
            calls.append((name, hermes_bin, timeout))
            return True, [], "ok"

        def callback():
            events.append("callback")
            return True

        runner, popen = self.runner(resolver, events=events)
        result = runner.run(
            "do the bounded task",
            toolsets=["context_engine"],
            expected_tools=[],
            policy=policy(),
            before_dispatch=callback,
        )
        self.assertEqual(result.status, "executed_unverified")
        self.assertFalse(result.verified)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.resolved_tools, ())
        self.assertEqual(result.route_provenance, "profile")
        self.assertEqual(events, ["resolve", "callback", "spawn"])
        self.assertEqual(calls, [("context_engine", "hermes", 5)])
        self.assertEqual(
            popen.argv,
            [
                "hermes", "-p", "default", "chat", "-Q", "--source", "tool",
                "--run-budget", "20", "--max-turns", "3", "-t",
                "context_engine,no_mcp", "--query-file", "-",
            ],
        )
        self.assertNotIn("--ignore-rules", popen.argv)
        self.assertNotIn("--yolo", popen.argv)
        self.assertFalse(popen.kwargs["shell"])
        self.assertTrue(popen.kwargs["start_new_session"])
        self.assertEqual(popen.kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(popen.process.communicate_calls[0][0], "do the bounded task")

    def test_each_toolset_is_resolved_separately_not_as_one_string(self):
        calls = []

        def resolver(name, *, hermes_bin, timeout):
            calls.append(name)
            return True, ["alpha"] if name == "one" else ["beta"], "ok"

        runner, popen = self.runner(resolver)
        result = runner.run(
            "task", toolsets=["one", "two"], expected_tools=["alpha", "beta"],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "executed_unverified")
        self.assertEqual(calls, ["one", "two"])
        self.assertIn("one,two,no_mcp", popen.argv)

    def test_sentinel_is_not_counted_even_if_resolver_reports_it(self):
        runner, _ = self.runner(lambda name, **kw: (True, ["no_mcp"], "ok"))
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "executed_unverified")
        self.assertEqual(result.resolved_tools, ())

    def test_registry_mismatch_denies_without_callback_or_spawn(self):
        events = []
        runner, popen = self.runner(
            lambda name, **kw: (True, ["safe_tool"], "ok"), events=events
        )
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(),
            before_dispatch=lambda: events.append("callback") or True,
        )
        self.assertEqual(result.status, "denied")
        self.assertEqual(events, [])
        self.assertFalse(hasattr(popen, "argv"))

    def test_resolver_failure_and_exception_deny(self):
        runner, popen = self.runner(lambda name, **kw: (False, [], "no runtime"))
        denied = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(denied.status, "denied")
        self.assertIn("no runtime", denied.reason)
        self.assertFalse(hasattr(popen, "argv"))

        def boom(name, **kw):
            raise RuntimeError("private resolver detail")

        runner, popen = self.runner(boom)
        denied = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(denied.status, "denied")
        self.assertIn("RuntimeError", denied.reason)
        self.assertNotIn("private resolver detail", denied.reason)
        self.assertFalse(hasattr(popen, "argv"))

    def test_unexpected_dangerous_exposure_denies_even_for_empty_expectation(self):
        runner, popen = self.runner(lambda name, **kw: (True, ["terminal"], "ok"))
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "denied")
        self.assertIn("unexpected tool exposure", result.reason)
        self.assertFalse(hasattr(popen, "argv"))

    def test_expected_tools_none_is_invalid_but_empty_list_is_valid(self):
        runner, _ = self.runner(lambda name, **kw: (True, [], "ok"))
        denied = runner.run(
            "task", toolsets=["context_engine"], expected_tools=None,
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(denied.status, "denied")
        self.assertIn("expected_tools is required", denied.reason)

    def test_broad_or_ambiguous_toolsets_are_rejected(self):
        runner, _ = self.runner(lambda name, **kw: (True, [], "ok"))
        for toolsets in (["all"], ["everything"], ["*"], "one,two", ["no_mcp"]):
            with self.subTest(toolsets=toolsets):
                result = runner.run(
                    "task", toolsets=toolsets, expected_tools=[],
                    policy=policy(), before_dispatch=lambda: True,
                )
                self.assertEqual(result.status, "denied")

    def test_dangerous_expected_tool_is_rejected(self):
        runner, _ = self.runner(lambda name, **kw: (True, ["write_file"], "ok"))
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=["write_file"],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "denied")
        self.assertIn("outside the bounded task scope", result.reason)

    def test_missing_false_and_raising_callback_deny_without_spawn(self):
        def resolver(name, **kw):
            return True, [], "ok"

        for callback in (None, lambda: False, lambda: 1):
            with self.subTest(callback=callback):
                runner, popen = self.runner(resolver)
                result = runner.run(
                    "task", toolsets=["context_engine"], expected_tools=[],
                    policy=policy(), before_dispatch=callback,
                )
                self.assertEqual(result.status, "denied")
                self.assertFalse(hasattr(popen, "argv"))

        def boom():
            raise RuntimeError("private pause state")

        runner, popen = self.runner(resolver)
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=boom,
        )
        self.assertEqual(result.status, "denied")
        self.assertIn("RuntimeError", result.reason)
        self.assertNotIn("private pause state", result.reason)
        self.assertFalse(hasattr(popen, "argv"))

    def test_policy_bounds_fail_closed_on_bool_zero_nan_and_malformed(self):
        runner, popen = self.runner(lambda name, **kw: (True, [], "ok"))
        cases = (
            None,
            {"evaluator": []},
            policy(max_turns=True),
            policy(max_turns=0),
            policy(run_budget_seconds=0),
            policy(run_budget_seconds=float("nan")),
            policy(timeout_seconds=float("inf")),
            policy(timeout_seconds=False),
            policy(resolve_timeout_seconds=0),
        )
        for item in cases:
            with self.subTest(policy=item):
                result = runner.run(
                    "task", toolsets=["context_engine"], expected_tools=[],
                    policy=item, before_dispatch=lambda: True,
                )
                self.assertEqual(result.status, "denied")
                self.assertFalse(hasattr(popen, "argv"))

    def test_profile_is_literal_validated_without_normalisation(self):
        runner, _ = self.runner(lambda name, **kw: (True, [], "ok"))
        for profile in (" default", "default ", "../default", "-default", "", "a/b"):
            with self.subTest(profile=profile):
                result = runner.run(
                    "task", toolsets=["context_engine"], expected_tools=[],
                    policy=policy(), profile=profile,
                    before_dispatch=lambda: True,
                )
                self.assertEqual(result.status, "denied")

    def test_route_requires_pair_and_reports_explicit_provenance(self):
        runner, _ = self.runner(lambda name, **kw: (True, [], "ok"))
        for route in ((("m",),), ("m", None), {"model": "m"}, {"model": "m", "provider": "p", "extra": "x"}):
            with self.subTest(route=route):
                result = runner.run(
                    "task", toolsets=["context_engine"], expected_tools=[],
                    policy=policy(), before_dispatch=lambda: True, route=route,
                )
                self.assertEqual(result.status, "denied")

        runner, popen = self.runner(lambda name, **kw: (True, [], "ok"))
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True, route=("model-a", "provider-a"),
        )
        self.assertEqual(result.status, "executed_unverified")
        self.assertEqual(result.route_provenance, "explicit")
        self.assertEqual(result.model, "model-a")
        self.assertEqual(result.provider, "provider-a")
        self.assertIn("--provider", popen.argv)
        self.assertIn("provider-a", popen.argv)
        self.assertIn("-m", popen.argv)
        self.assertIn("model-a", popen.argv)

    def test_timeout_terminates_owned_process_group_with_term_then_kill(self):
        events = []
        kills = []
        process = FakeProcess(returncode=-9, timeout_calls=2)
        runner, _ = self.runner(
            lambda name, **kw: (True, [], "ok"), popen=process,
            killpg=lambda pid, sig: (events.append("killpg"), kills.append((pid, sig))),
            events=events,
        )
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(timeout_seconds=1), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "timeout")
        self.assertFalse(result.verified)
        self.assertEqual(
            kills,
            [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)],
        )
        self.assertEqual(len(process.communicate_calls), 3)
        self.assertIn("process_group_terminated", result.diagnostics)

    def test_nonzero_exit_is_error_not_action_success(self):
        process = FakeProcess(stdout="provider text", stderr="private failure", returncode=7)
        runner, _ = self.runner(lambda name, **kw: (True, [], "ok"), popen=process)
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "error")
        self.assertEqual(result.exit_code, 7)
        self.assertFalse(result.verified)
        self.assertNotIn("private failure", result.reason)
        self.assertEqual(result.output, "provider text")

    def test_output_and_instruction_retention_are_bounded(self):
        process = FakeProcess(stdout="x" * (MAX_OUTPUT_CHARS + 10))
        runner, _ = self.runner(lambda name, **kw: (True, [], "ok"), popen=process)
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "executed_unverified")
        self.assertTrue(result.output_truncated)
        self.assertEqual(len(result.output), MAX_OUTPUT_CHARS)

        runner, popen = self.runner(lambda name, **kw: (True, [], "ok"))
        result = runner.run(
            "x" * (MAX_INSTRUCTIONS_CHARS + 1),
            toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "denied")
        self.assertFalse(hasattr(popen, "argv"))

    def test_spawn_failure_is_reported_without_a_fake_result(self):
        events = []

        def failing_popen(argv, **kwargs):
            events.append("spawn")
            raise FileNotFoundError("missing")

        runner = NativeTaskRunner(
            resolver=lambda name, **kw: (True, [], "ok"),
            popen_factory=failing_popen,
            clock=lambda: 100.0,
        )
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "error")
        self.assertIn("FileNotFoundError", result.reason)
        self.assertEqual(result.output, "")
        self.assertEqual(events, ["spawn"])


if __name__ == "__main__":
    unittest.main()
