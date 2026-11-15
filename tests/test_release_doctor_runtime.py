"""Focused runtime contract tests for the extracted-package native doctor.

These tests mock only subprocess execution.  The packaging test in
``test_main_final_paths.py`` remains the real end-to-end validator.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import release_export


class _FakeProcess:
    def __init__(self, command, stdout, stderr, outcome):
        self.command = command
        self.pid = 424242
        self.returncode = outcome.get("returncode", 0)
        self._stdout = stdout
        self._stderr = stderr
        self._outcome = outcome
        self.wait_calls = []

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self._outcome.get("timeout") and len(self.wait_calls) == 1:
            raise subprocess.TimeoutExpired(self.command, timeout)
        self._stdout.write(self._outcome.get("stdout", "validator ok\n"))
        self._stderr.write(self._outcome.get("stderr", ""))
        if self._outcome.get("timeout"):
            self.returncode = -9
        return self.returncode

    def poll(self):
        return self.returncode


class NativeDoctorRuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="secretary-doctor-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.installed = self.root / "installed"
        self.installed.mkdir()
        self.package = self.root / "native" / "hermes_cli"
        self.package.mkdir(parents=True)
        (self.package / "main.py").write_text("# native entrypoint\n", encoding="utf-8")
        self.spec = SimpleNamespace(
            submodule_search_locations=[str(self.package)], origin=None)
        self.popen_calls = []
        self.env_at_launch = None
        self.isolated_dirs_existed = None
        self.outcome = {"returncode": 0, "stdout": "validator ok\n"}

    def fake_popen(self, command, **kwargs):
        self.popen_calls.append((command, kwargs))
        self.env_at_launch = dict(kwargs["env"])
        self.isolated_dirs_existed = (
            Path(kwargs["env"]["HOME"]).is_dir(),
            Path(kwargs["env"]["HERMES_HOME"]).is_dir(),
            Path(kwargs["env"]["TMPDIR"]).is_dir(),
        )
        return _FakeProcess(command, kwargs["stdout"], kwargs["stderr"],
                            self.outcome)

    def run_doctor(self):
        with mock.patch.object(release_export.importlib.util, "find_spec",
                               return_value=self.spec), \
                mock.patch.object(release_export.subprocess, "Popen",
                                  side_effect=self.fake_popen), \
                mock.patch.object(release_export, "_kill_process_group") as kill, \
                mock.patch.object(release_export.shutil, "which",
                                  side_effect=AssertionError(
                                      "must not resolve the published hermes launcher")):
            result = release_export._native_host_doctor(self.installed)
        return result, kill

    def test_uses_current_interpreter_module_and_isolated_profile(self):
        before = dict(os.environ)
        result, _kill = self.run_doctor()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["launch_mode"], "python-module")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(dict(os.environ), before)

        command, kwargs = self.popen_calls[0]
        self.assertEqual(command, [
            sys.executable, "-m", "hermes_cli.main", "plugins", "doctor",
            "--ci", str(self.installed)])
        self.assertEqual(command[0], sys.executable)
        self.assertNotEqual(Path(command[0]).name, "hermes")
        self.assertTrue(kwargs["start_new_session"])

        env = self.env_at_launch
        self.assertNotEqual(env["HOME"], before.get("HOME"))
        self.assertNotEqual(env["HERMES_HOME"], before.get("HERMES_HOME"))
        self.assertNotEqual(env["TMPDIR"], before.get("TMPDIR"))
        self.assertEqual(self.isolated_dirs_existed, (True, True, True))
        self.assertIn(str(self.package.parent),
                      env["PYTHONPATH"].split(os.pathsep))

    def test_missing_native_sdk_is_reported_without_launcher_fallback(self):
        with mock.patch.object(release_export.importlib.util, "find_spec",
                               return_value=None), \
                mock.patch.object(release_export.subprocess, "Popen",
                                  side_effect=AssertionError("must not launch")) as popen:
            result = release_export._native_host_doctor(self.installed)
        self.assertFalse(result["available"])
        self.assertFalse(result["ok"])
        self.assertIn("not importable", result["reason"])
        self.assertEqual(popen.call_count, 0)

    def test_native_nonzero_is_a_failure_with_real_return_details(self):
        self.outcome = {"returncode": 7, "stdout": "validator stdout\n",
                        "stderr": "validator stderr\n"}
        result, _kill = self.run_doctor()
        self.assertTrue(result["available"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["returncode"], 7)
        self.assertIn("validator stdout", result["detail"])
        self.assertEqual(result["stdout"], "validator stdout\n")
        self.assertEqual(result["stderr"], "validator stderr\n")
        self.assertEqual(len(self.popen_calls), 1)
        command = self.popen_calls[0][0]
        self.assertEqual(command[1:3], ["-m", "hermes_cli.main"])

    def test_native_timeout_is_a_failure_and_the_child_is_reaped(self):
        self.outcome = {"timeout": True, "returncode": 0,
                        "stdout": "partial validator output\n"}
        result, kill = self.run_doctor()
        self.assertTrue(result["available"])
        self.assertFalse(result["ok"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["reason"], "TimeoutExpired")
        self.assertEqual(result["returncode"], -9)
        self.assertIn("partial validator output", result["detail"])
        self.assertGreaterEqual(kill.call_count, 1)
        self.assertEqual(len(self.popen_calls), 1)
        command = self.popen_calls[0][0]
        self.assertEqual(command[1:3], ["-m", "hermes_cli.main"])
        self.assertFalse(any("install" in part for part in command[1:3]))


if __name__ == "__main__":
    unittest.main()
