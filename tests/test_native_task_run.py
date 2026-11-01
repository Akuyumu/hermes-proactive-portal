"""Product unit tests using controlled low-level doubles only."""
from __future__ import annotations

import errno
import signal
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from proactive_pilot import native_task_run as ntr
from proactive_pilot.native_task_run import (
    MAX_INSTRUCTIONS_CHARS,
    MAX_OUTPUT_CHARS,
    NativeTaskRunner,
)


class FakeStream:
    """Bounded-read stream double: unbounded reads are a contract violation."""

    def __init__(self, data=""):
        self._data = data
        self.read_sizes = []
        self.writes = []
        self.closed = False

    def read(self, size=-1):
        if size is None or size < 0:
            raise AssertionError("production must not perform an unbounded read")
        self.read_sizes.append(size)
        if not self._data:
            return ""
        chunk = self._data[:size]
        self._data = self._data[size:]
        return chunk

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True

    def getvalue(self):
        return "".join(self.writes)


class FakeProcess:
    def __init__(self, *, stdout="", stderr="", returncode=0, wait_timeouts=0):
        self.pid = 4321
        self.returncode = returncode
        self.stdin = FakeStream()
        self.stdout = FakeStream(stdout)
        self.stderr = FakeStream(stderr)
        self._wait_timeouts = wait_timeouts
        self._exited = False
        self.wait_calls = []
        self.group_alive = False

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self._wait_timeouts > 0:
            self._wait_timeouts -= 1
            raise subprocess.TimeoutExpired(cmd="hermes", timeout=timeout)
        self._exited = True
        return self.returncode

    def poll(self):
        return self.returncode if self._exited else None


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
    def runner(self, resolver, popen=None, killpg=None, events=None, group_probe=None):
        events = events if events is not None else []
        process = popen or FakeProcess()
        fake_popen = FakePopen(process, events)
        return NativeTaskRunner(
            resolver=resolver,
            popen_factory=fake_popen,
            killpg=killpg or (lambda pid, sig: None),
            group_probe=group_probe or (lambda pid: process.group_alive),
            clock=lambda: 100.0,
        ), fake_popen

    def test_zero_tool_success_is_executed_unverified_and_ordered(self):
        events = []
        calls = []

        def resolver(toolsets, *, hermes_bin, timeout, profile):
            events.append("resolve")
            calls.append((toolsets, hermes_bin, timeout, profile))
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
        self.assertEqual(calls, [(("context_engine",), "hermes", 5, "default")])
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
        self.assertEqual(popen.process.stdin.getvalue(), "do the bounded task")

    def test_selected_profile_is_used_for_resolution_and_child(self):
        calls = []

        def resolver(toolsets, *, hermes_bin, timeout, profile):
            calls.append((toolsets, profile))
            return True, [], "ok"

        runner, popen = self.runner(resolver)
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), profile="work", before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "executed_unverified")
        self.assertEqual(calls, [(("context_engine",), "work")])
        self.assertEqual(popen.argv[:4], ["hermes", "-p", "work", "chat"])

    def test_combined_explicit_toolsets_are_resolved_once(self):
        calls = []

        def resolver(toolsets, *, hermes_bin, timeout, profile):
            calls.append((toolsets, hermes_bin, timeout, profile))
            return True, ["alpha", "beta"], "ok"

        runner, popen = self.runner(resolver)
        result = runner.run(
            "task", toolsets=["one", "two"], expected_tools=["alpha", "beta"],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "executed_unverified")
        self.assertEqual(calls, [(("one", "two"), "hermes", 5, "default")])
        self.assertIn("one,two,no_mcp", popen.argv)

    def test_explicit_empty_scope_still_calls_resolver_with_empty_selection(self):
        calls = []

        def resolver(toolsets, *, hermes_bin, timeout, profile):
            calls.append((toolsets, profile))
            return True, [], "ok"

        runner, popen = self.runner(resolver)
        result = runner.run(
            "task", toolsets=[], expected_tools=[], policy=policy(),
            before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "executed_unverified")
        self.assertEqual(calls, [((), "default")])
        self.assertEqual(result.resolved_tools, ())
        self.assertIn("no_mcp", popen.argv)
        self.assertNotIn(",no_mcp", popen.argv)

    def test_resolver_reports_sentinel_without_counting_it(self):
        runner, _ = self.runner(lambda toolsets, **kw: (True, ["no_mcp"], "ok"))
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "executed_unverified")
        self.assertEqual(result.resolved_tools, ())

    def test_registry_mismatch_denies_without_callback_or_spawn(self):
        events = []
        runner, popen = self.runner(
            lambda toolsets, **kw: (True, ["safe_tool"], "ok"), events=events
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
        runner, popen = self.runner(lambda toolsets, **kw: (False, [], "no runtime"))
        denied = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(denied.status, "denied")
        self.assertIn("no runtime", denied.reason)
        self.assertFalse(hasattr(popen, "argv"))

        def boom(toolsets, **kw):
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
        runner, popen = self.runner(lambda toolsets, **kw: (True, ["terminal"], "ok"))
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "denied")
        self.assertIn("unexpected tool exposure", result.reason)
        self.assertFalse(hasattr(popen, "argv"))

    def test_expected_tools_none_is_invalid_but_empty_list_is_valid(self):
        runner, _ = self.runner(lambda toolsets, **kw: (True, [], "ok"))
        denied = runner.run(
            "task", toolsets=["context_engine"], expected_tools=None,
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(denied.status, "denied")
        self.assertIn("expected_tools is required", denied.reason)

    def test_broad_or_ambiguous_toolsets_are_rejected(self):
        runner, _ = self.runner(lambda toolsets, **kw: (True, [], "ok"))
        for toolsets in (["all"], ["everything"], ["*"], "one,two", ["no_mcp"]):
            with self.subTest(toolsets=toolsets):
                result = runner.run(
                    "task", toolsets=toolsets, expected_tools=[],
                    policy=policy(), before_dispatch=lambda: True,
                )
                self.assertEqual(result.status, "denied")

    def test_dangerous_expected_tool_is_rejected(self):
        runner, _ = self.runner(lambda toolsets, **kw: (True, ["write_file"], "ok"))
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=["write_file"],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "denied")
        self.assertIn("outside the bounded task scope", result.reason)

    def test_allowlisted_secretary_tools_reach_controlled_spawn(self):
        for tool in ("secretary_task_inspect", "secretary_task_action"):
            with self.subTest(tool=tool):
                events = []
                calls = []

                def resolver(toolsets, *, hermes_bin, timeout, profile):
                    events.append("resolve")
                    calls.append((toolsets, hermes_bin, timeout, profile))
                    return True, [tool], "ok"

                runner, popen = self.runner(resolver, events=events)
                result = runner.run(
                    "inspect the bounded task",
                    toolsets=["secretary_background"],
                    expected_tools=[tool],
                    policy=policy(),
                    before_dispatch=lambda: events.append("callback") or True,
                )
                self.assertEqual(result.status, "executed_unverified")
                self.assertEqual(result.toolsets, ("secretary_background",))
                self.assertEqual(result.expected_tools, (tool,))
                self.assertEqual(result.resolved_tools, (tool,))
                self.assertEqual(events, ["resolve", "callback", "spawn"])
                self.assertEqual(
                    calls,
                    [(("secretary_background",), "hermes", 5, "default")],
                )
                self.assertIn("secretary_background,no_mcp", popen.argv)

    def test_secretary_registry_mismatch_denies_before_callback_or_spawn(self):
        events = []

        def resolver(toolsets, **kwargs):
            events.append("resolve")
            return True, ["secretary_task_inspect", "unexpected_safe_tool"], "ok"

        runner, popen = self.runner(resolver, events=events)
        result = runner.run(
            "task", toolsets=["secretary_background"],
            expected_tools=["secretary_task_inspect"],
            policy=policy(),
            before_dispatch=lambda: events.append("callback") or True,
        )
        self.assertEqual(result.status, "denied")
        self.assertIn("registry mismatch", result.reason)
        self.assertEqual(events, ["resolve"])
        self.assertFalse(hasattr(popen, "argv"))

    def test_dangerous_tool_words_are_denied_without_resolution(self):
        cases = (
            "terminal", "read_file", "write_file",
            "mcp__filesystem__read_file", "send_message", "secrets",
            "credentials", "readFile", "writeFile", "mcpServer",
            "sendMessage", "secretStore", "credentialStore",
        )
        for tool in cases:
            with self.subTest(tool=tool):
                events = []

                def resolver(toolsets, **kwargs):
                    events.append("resolve")
                    return True, [tool], "ok"

                runner, popen = self.runner(resolver, events=events)
                result = runner.run(
                    "task", toolsets=["context_engine"], expected_tools=[tool],
                    policy=policy(),
                    before_dispatch=lambda: events.append("callback") or True,
                )
                self.assertEqual(result.status, "denied")
                self.assertIn("outside the bounded task scope", result.reason)
                self.assertEqual(events, [])
                self.assertFalse(hasattr(popen, "argv"))

    def test_missing_false_and_raising_callback_deny_without_spawn(self):
        def resolver(toolsets, **kw):
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
        runner, popen = self.runner(lambda toolsets, **kw: (True, [], "ok"))
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
        runner, _ = self.runner(lambda toolsets, **kw: (True, [], "ok"))
        for profile in (" default", "default ", "../default", "-default", "", "a/b"):
            with self.subTest(profile=profile):
                result = runner.run(
                    "task", toolsets=["context_engine"], expected_tools=[],
                    policy=policy(), profile=profile,
                    before_dispatch=lambda: True,
                )
                self.assertEqual(result.status, "denied")

    def test_route_requires_pair_and_reports_explicit_provenance(self):
        runner, _ = self.runner(lambda toolsets, **kw: (True, [], "ok"))
        for route in ((("m",),), ("m", None), {"model": "m"}, {"model": "m", "provider": "p", "extra": "x"}):
            with self.subTest(route=route):
                result = runner.run(
                    "task", toolsets=["context_engine"], expected_tools=[],
                    policy=policy(), before_dispatch=lambda: True, route=route,
                )
                self.assertEqual(result.status, "denied")

        runner, popen = self.runner(lambda toolsets, **kw: (True, [], "ok"))
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
        process = FakeProcess(returncode=-9, wait_timeouts=1)
        process.group_alive = True

        def kill_group(pid, sig):
            events.append("killpg")
            kills.append((pid, sig))
            if sig == signal.SIGKILL:
                process.group_alive = False

        runner, _ = self.runner(
            lambda toolsets, **kw: (True, [], "ok"), popen=process,
            killpg=kill_group, events=events,
        )
        with mock.patch.object(ntr, "TERM_GRACE_SECONDS", 0.01), \
                mock.patch.object(ntr, "KILL_GRACE_SECONDS", 0.01):
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
        self.assertIn("process_group_terminated", result.diagnostics)
        self.assertNotIn("process_group_termination_unconfirmed", result.diagnostics)

    def test_failed_group_kill_is_not_reported_as_confirmed_termination(self):
        process = FakeProcess(returncode=-9, wait_timeouts=1)
        process.group_alive = True

        def denied_killpg(pid, sig):
            raise OSError("not permitted")

        runner, _ = self.runner(
            lambda toolsets, **kw: (True, [], "ok"), popen=process,
            killpg=denied_killpg,
        )
        with mock.patch.object(ntr, "TERM_GRACE_SECONDS", 0.01), \
                mock.patch.object(ntr, "KILL_GRACE_SECONDS", 0.01):
            result = runner.run(
                "task", toolsets=["context_engine"], expected_tools=[],
                policy=policy(timeout_seconds=1), before_dispatch=lambda: True,
            )
        self.assertEqual(result.status, "timeout")
        self.assertNotIn("process_group_terminated", result.diagnostics)
        self.assertIn("process_group_termination_unconfirmed", result.diagnostics)

    def test_exited_leader_with_live_descendant_still_gets_group_killed(self):
        process = FakeProcess(returncode=0)
        process._exited = True  # leader is already reaped; the group is not
        process.group_alive = True
        kills = []

        def kill_group(pid, sig):
            kills.append(sig)
            if sig == signal.SIGKILL:
                process.group_alive = False

        runner, _ = self.runner(
            lambda toolsets, **kw: (True, [], "ok"), popen=process,
            killpg=kill_group,
        )
        with mock.patch.object(ntr, "TERM_GRACE_SECONDS", 0.01), \
                mock.patch.object(ntr, "KILL_GRACE_SECONDS", 0.01):
            confirmed, label = runner._terminate(process)
        self.assertTrue(confirmed)
        self.assertEqual(label, "sigkill")
        self.assertEqual(kills, [signal.SIGTERM, signal.SIGKILL])
        self.assertFalse(process.group_alive)

    def test_group_probe_generic_oserror_is_not_confirmed_absence(self):
        proc = SimpleNamespace(pid=7654321, poll=lambda: 0)
        with mock.patch(
            "os.killpg",
            side_effect=OSError(errno.EIO, "controlled probe failure"),
        ) as killpg:
            with mock.patch.object(ntr, "TERM_GRACE_SECONDS", 0.01), \
                    mock.patch.object(ntr, "KILL_GRACE_SECONDS", 0.01):
                confirmed, label = NativeTaskRunner()._terminate(proc)
        self.assertFalse(confirmed)
        self.assertEqual(label, "unconfirmed")
        self.assertTrue(killpg.called)

    def test_group_probe_process_lookup_confirms_absence(self):
        proc = SimpleNamespace(pid=7654321, poll=lambda: 0)
        with mock.patch("os.killpg", side_effect=ProcessLookupError()):
            confirmed, label = NativeTaskRunner()._terminate(proc)
        self.assertTrue(confirmed)
        self.assertEqual(label, "already_exited")

    def test_group_probe_recovers_from_unknown_error_to_absence(self):
        proc = SimpleNamespace(pid=7654321, poll=lambda: 0)
        probe_calls = {"count": 0}

        def controlled_killpg(pid, sig):
            if sig != 0:
                return None
            probe_calls["count"] += 1
            if probe_calls["count"] == 1:
                raise OSError(errno.EIO, "controlled probe failure")
            if probe_calls["count"] == 2:
                return None
            raise ProcessLookupError()

        with mock.patch("os.killpg", side_effect=controlled_killpg):
            with mock.patch.object(ntr, "TERM_GRACE_SECONDS", 0.01), \
                    mock.patch.object(ntr, "KILL_GRACE_SECONDS", 0.01):
                confirmed, label = NativeTaskRunner()._terminate(proc)
        self.assertTrue(confirmed)
        self.assertEqual(label, "sigterm")
        self.assertGreaterEqual(probe_calls["count"], 3)

    def test_nonzero_exit_is_error_not_action_success(self):
        process = FakeProcess(stdout="provider text", stderr="private failure", returncode=7)
        runner, _ = self.runner(lambda toolsets, **kw: (True, [], "ok"), popen=process)
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
        runner, _ = self.runner(lambda toolsets, **kw: (True, [], "ok"), popen=process)
        result = runner.run(
            "task", toolsets=["context_engine"], expected_tools=[],
            policy=policy(), before_dispatch=lambda: True,
        )
        self.assertEqual(result.status, "executed_unverified")
        self.assertTrue(result.output_truncated)
        self.assertEqual(len(result.output), MAX_OUTPUT_CHARS)
        self.assertTrue(process.stdout.read_sizes)
        self.assertTrue(all(size == 8192 for size in process.stdout.read_sizes))

        runner, popen = self.runner(lambda toolsets, **kw: (True, [], "ok"))
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
            resolver=lambda toolsets, **kw: (True, [], "ok"),
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
