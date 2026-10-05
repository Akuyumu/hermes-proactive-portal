"""Optional Google Workspace calendar adapter: probe, argv, parse, fail-closed.

No subprocess, no network, no token read. The runner is fake and the HERMES_HOME
is a workspace temp dir.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import gws_calendar as gws  # noqa: E402


class _Clock:
    def now(self):
        return datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


class _Runner:
    def __init__(self, rc=0, stdout="[]", stderr=""):
        self.rc, self.stdout, self.stderr = rc, stdout, stderr
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))

        class Proc:
            returncode = self.rc
            stdout = self.stdout
            stderr = self.stderr

        return Proc()


def _home(tmp: str, *, script=True, token=True) -> Path:
    home = Path(tmp) / "home"
    skill = home / gws.SKILL_RELATIVE
    skill.parent.mkdir(parents=True, exist_ok=True)
    if script:
        skill.write_text("# stub\n", encoding="utf-8")
    if token:
        (home / "google_token.json").write_text(json.dumps({"token": "do-not-read"}),
                                                encoding="utf-8")
    return home


class ReadinessTests(unittest.TestCase):
    def test_missing_skill_is_not_ready(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            state = gws.readiness(hermes_home=Path(tmp) / "home", which=lambda n: "/gws")
        self.assertFalse(state["ready"])
        self.assertIn("skill", state["reason"])

    def test_missing_dependency_is_not_ready(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home = _home(tmp)
            with mock.patch.object(gws, "_find_spec", return_value=False):
                state = gws.readiness(hermes_home=home, which=lambda n: None)
        self.assertFalse(state["ready"])
        self.assertIn("client", state["reason"])

    def test_ready_with_script_token_and_backend(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home = _home(tmp)
            state = gws.readiness(hermes_home=home, which=lambda n: "/usr/bin/gws")
        self.assertTrue(state["ready"])
        self.assertEqual(state["backend"], "gws")

    def test_probe_never_reads_token_value(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            home = _home(tmp)
            with mock.patch("builtins.open", side_effect=AssertionError("token read")):
                state = gws.readiness(hermes_home=home, which=lambda n: "/usr/bin/gws")
        self.assertTrue(state["ready"])


class AdapterTests(unittest.TestCase):
    def _adapter(self, home, runner, *, enabled=True):
        policy = {"gws_calendar_enabled": enabled, "calendar_ids": ["primary"]}
        return gws.GwsCalendarAdapter(policy, hermes_home=home, runner=runner,
                                      which=lambda n: "/usr/bin/gws")

    def test_disabled_capability_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            adapter = self._adapter(_home(tmp), _Runner(), enabled=False)
            with self.assertRaises(gws.GwsUnsupported) as ctx:
                adapter.list_events(start="a", end="b")
        self.assertIn("disabled", str(ctx.exception))

    def test_account_routing_unsupported(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            adapter = self._adapter(_home(tmp), _Runner())
            with self.assertRaises(gws.GwsUnsupported):
                adapter.list_events(account="other-user", start="a", end="b")

    def test_successful_list_parses_absolute_offsets(self):
        payload = json.dumps([{
            "id": "e1", "summary": "Standup",
            "start": "2026-10-06T09:00:00+08:00",
            "end": "2026-10-06T09:30:00+08:00",
            "location": "Room", "status": "confirmed",
        }])
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            runner = _Runner(stdout=payload)
            adapter = self._adapter(_home(tmp), runner)
            events = adapter.list_events(start="2026-10-06T00:00:00Z",
                                         end="2026-10-07T00:00:00Z")
        self.assertEqual(len(events), 1)
        self.assertIn("+08:00", events[0].start)
        self.assertFalse(events[0].all_day)
        argv = runner.calls[0][0]
        self.assertIn("calendar", argv)
        self.assertIn("list", argv)
        self.assertIn("--start", argv)
        self.assertIn("--end", argv)
        self.assertIn("--calendar", argv)
        self.assertEqual(argv[argv.index("--calendar") + 1], "primary")

    def test_all_day_event_is_accepted(self):
        payload = json.dumps([{"id": "e2", "summary": "Holiday",
                               "start": "2026-10-06", "end": "2026-10-07"}])
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            adapter = self._adapter(_home(tmp), _Runner(stdout=payload))
            events = adapter.list_events(start="a", end="b")
        self.assertTrue(events[0].all_day)

    def test_malformed_json_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            adapter = self._adapter(_home(tmp), _Runner(stdout="not json"))
            with self.assertRaises(gws.GwsUnsupported):
                adapter.list_events(start="a", end="b")

    def test_missing_offset_fails_closed(self):
        payload = json.dumps([{"id": "e3", "start": "2026-10-06T09:00:00",
                               "end": "2026-10-06T09:30:00"}])
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            adapter = self._adapter(_home(tmp), _Runner(stdout=payload))
            with self.assertRaises(gws.GwsUnsupported):
                adapter.list_events(start="a", end="b")

    def test_nonzero_exit_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            adapter = self._adapter(_home(tmp), _Runner(rc=2, stdout="", stderr="403"))
            with self.assertRaises(gws.GwsUnsupported) as ctx:
                adapter.list_events(start="a", end="b")
        self.assertIn("failed", str(ctx.exception))

    def test_unready_adapter_does_not_run(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            runner = _Runner()
            adapter = self._adapter(Path(tmp) / "home", runner)  # no script/token
            with self.assertRaises(gws.GwsUnsupported):
                adapter.list_events(start="a", end="b")
        self.assertEqual(runner.calls, [])

    def test_collect_returns_source_error_when_unavailable(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            from proactive_pilot.collectors import SourceResult
            adapter = self._adapter(Path(tmp) / "home", _Runner())
            result = adapter.collect(_Clock(), {"calendar_ids": ["primary"]})
        self.assertIsInstance(result, SourceResult)
        self.assertEqual(result.status, "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
