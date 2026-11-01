"""Boundary controls for profile scoping and bounded native resolution."""
from __future__ import annotations

import errno
import sys
import types
import unittest
from unittest import mock

from proactive_pilot import evaluator


class BoundedStream:
    def __init__(self, data=""):
        self._data = data
        self.read_sizes = []
        self.closed = False

    def read(self, size=-1):
        if size is None or size < 0:
            raise AssertionError("resolver must not perform an unbounded read")
        self.read_sizes.append(size)
        if not self._data:
            return ""
        chunk = self._data[:size]
        self._data = self._data[size:]
        return chunk

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, pid=9876, returncode=None):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode


class ProfileModules:
    """Install synthetic native modules; no real profile or account is read."""

    def __init__(self, *, active_home="/home/u/.hermes", selected_home="/home/u/.hermes"):
        self.model_calls = []
        self.active_home = active_home
        self.selected_home = selected_home

    def __enter__(self):
        model_tools = types.ModuleType("model_tools")

        def get_tool_definitions(enabled_toolsets=None, quiet_mode=False, **_kw):
            self.model_calls.append(list(enabled_toolsets or []))
            return []

        model_tools.get_tool_definitions = get_tool_definitions

        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: self.active_home
        constants.get_default_hermes_root = lambda: self.active_home

        profiles = types.ModuleType("hermes_cli.profiles")
        profiles.resolve_profile_env = lambda name: self.selected_home

        package = types.ModuleType("hermes_cli")
        package.__path__ = []
        package.profiles = profiles

        self._stack = mock.patch.dict(
            sys.modules,
            {
                "model_tools": model_tools,
                "hermes_constants": constants,
                "hermes_cli": package,
                "hermes_cli.profiles": profiles,
            },
        )
        self._stack.start()
        return self

    def __exit__(self, *exc):
        self._stack.stop()
        return False


class ResolverBoundaryTests(unittest.TestCase):
    def test_legacy_single_toolset_caller_still_works(self):
        with ProfileModules() as modules:
            ok, names, detail = evaluator.resolve_toolset_tools(
                "context_engine", timeout=1
            )
        self.assertTrue(ok)
        self.assertEqual(names, [])
        self.assertEqual(detail, "in-process")
        self.assertEqual(modules.model_calls, [["context_engine"]])

    def test_ambient_process_cannot_certify_a_different_selected_profile(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if "--print-runtime-command" in argv:
                return (
                    0,
                    '["/venv/bin/python", "-c", "sys.path.insert(0, \'/agent\')"]',
                    "",
                    False,
                    False,
                )
            return (0, 'RESOLVED_JSON {"names": ["secretary_task_inspect"]}', "", False, False)

        with ProfileModules(
            active_home="/home/u/.hermes", selected_home="/home/u/.hermes/profiles/work"
        ) as modules:
            with mock.patch.object(evaluator, "_run_bounded", side_effect=fake_run):
                ok, names, detail = evaluator.resolve_toolset_tools(
                    None,
                    toolsets=["secretary_background"],
                    profile="work",
                    timeout=5,
                )

        self.assertTrue(ok)
        self.assertEqual(names, ["secretary_task_inspect"])
        self.assertEqual(detail, "subprocess")
        self.assertEqual(modules.model_calls, [])  # ambient registry was NOT trusted
        self.assertEqual(len(calls), 2)
        code = calls[1][3]
        self.assertIn("resolve_profile_env('work')", code)
        self.assertIn("enabled_toolsets=['secretary_background']", code)

    def test_explicit_empty_scope_resolves_as_empty_not_default(self):
        with ProfileModules() as modules:
            with mock.patch.object(evaluator, "_profile_matches_active", return_value=True):
                ok, names, detail = evaluator.resolve_toolset_tools(
                    None, toolsets=[], profile="work", timeout=5
                )
        self.assertTrue(ok)
        self.assertEqual(names, [])
        self.assertEqual(detail, "in-process")
        self.assertEqual(modules.model_calls, [[]])

    def test_resolver_timeout_reports_unconfirmed_termination(self):
        def fake_run(argv, **kwargs):
            if "--print-runtime-command" in argv:
                return (
                    0,
                    '["/venv/bin/python", "-c", "sys.path.insert(0, \'/agent\')"]',
                    "",
                    False,
                    False,
                )
            return (None, "", "", True, False)

        with ProfileModules(selected_home="/home/u/.hermes/profiles/work"):
            with mock.patch.object(evaluator, "_run_bounded", side_effect=fake_run):
                ok, names, detail = evaluator.resolve_toolset_tools(
                    None, toolsets=["secretary_background"], profile="work", timeout=5
                )
        self.assertFalse(ok)
        self.assertEqual(names, [])
        self.assertIn("timed out", detail)
        self.assertIn("termination unconfirmed", detail)
        self.assertNotIn("(terminated)", detail)

    def test_failed_group_signal_is_never_confirmed(self):
        proc = FakeProc()
        with mock.patch.object(evaluator, "_signal_group", return_value=False):
            with mock.patch.object(evaluator, "_group_alive", return_value=True):
                with mock.patch.object(evaluator, "_GROUP_TERM_GRACE_SECONDS", 0.01), \
                        mock.patch.object(evaluator, "_GROUP_KILL_GRACE_SECONDS", 0.01):
                    confirmed, label = evaluator._terminate_group(proc)
        self.assertFalse(confirmed)
        self.assertEqual(label, "unconfirmed")

    def test_exited_leader_with_live_descendant_kills_owned_group(self):
        proc = FakeProc(returncode=0)  # leader.poll() already reports exit
        alive = {"value": True}
        signals = []

        def signal_group(_proc, sig):
            signals.append(sig)
            if sig == evaluator.signal.SIGKILL:
                alive["value"] = False
            return True

        with mock.patch.object(evaluator, "_signal_group", side_effect=signal_group):
            with mock.patch.object(
                evaluator, "_group_alive", side_effect=lambda _proc: alive["value"]
            ):
                with mock.patch.object(evaluator, "_GROUP_TERM_GRACE_SECONDS", 0.01), \
                        mock.patch.object(evaluator, "_GROUP_KILL_GRACE_SECONDS", 0.01):
                    confirmed, label = evaluator._terminate_group(proc)
        self.assertTrue(confirmed)
        self.assertEqual(label, "sigkill")
        self.assertEqual(
            signals, [evaluator.signal.SIGTERM, evaluator.signal.SIGKILL]
        )
        self.assertFalse(alive["value"])

    def test_group_probe_generic_oserror_is_not_confirmed_absence(self):
        proc = FakeProc(pid=7654321, returncode=0)
        with mock.patch(
            "os.killpg",
            side_effect=OSError(errno.EIO, "controlled probe failure"),
        ) as killpg:
            with mock.patch.object(evaluator, "_GROUP_TERM_GRACE_SECONDS", 0.01), \
                    mock.patch.object(evaluator, "_GROUP_KILL_GRACE_SECONDS", 0.01):
                confirmed, label = evaluator._terminate_group(proc)
        self.assertFalse(confirmed)
        self.assertEqual(label, "unconfirmed")
        self.assertTrue(killpg.called)

    def test_group_probe_process_lookup_confirms_absence(self):
        proc = FakeProc(pid=7654321, returncode=0)
        with mock.patch("os.killpg", side_effect=ProcessLookupError()):
            confirmed, label = evaluator._terminate_group(proc)
        self.assertTrue(confirmed)
        self.assertEqual(label, "already_exited")

    def test_group_probe_recovers_from_unknown_error_to_absence(self):
        proc = FakeProc(pid=7654321, returncode=0)
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
            with mock.patch.object(evaluator, "_GROUP_TERM_GRACE_SECONDS", 0.01), \
                    mock.patch.object(evaluator, "_GROUP_KILL_GRACE_SECONDS", 0.01):
                confirmed, label = evaluator._terminate_group(proc)
        self.assertTrue(confirmed)
        self.assertEqual(label, "sigterm")
        self.assertGreaterEqual(probe_calls["count"], 3)

    def test_collect_stream_caps_retention_while_draining(self):
        stream = BoundedStream("x" * (evaluator._PROBE_STREAM_LIMIT + 100))
        kept, total, error = evaluator._collect_stream(
            stream, evaluator._PROBE_STREAM_LIMIT
        )
        self.assertIsNone(error)
        self.assertEqual(len(kept), evaluator._PROBE_STREAM_LIMIT)
        self.assertEqual(total, evaluator._PROBE_STREAM_LIMIT + 100)
        self.assertTrue(stream.closed)
        self.assertTrue(all(size == 8192 for size in stream.read_sizes))


if __name__ == "__main__":
    unittest.main()
