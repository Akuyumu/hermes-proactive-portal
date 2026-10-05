"""Reminder event authorisation must mirror the migration's discovery marker.

A legacy policy that omitted account scope is migrated to an explicit marker
(``advanced.legacy_account_discovery``) plus empty account lists. Monitoring
honours the marker by not narrowing the policy keys; the reminder path must
agree, otherwise event reminders silently lose authorisation while monitoring
still reads the same calendars.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import reminders as R  # noqa: E402
from proactive_pilot import userconfig as uc  # noqa: E402


class DiscoveryAuthorisationParity(unittest.TestCase):
    def _policy_file(self, tmp: Path) -> Path:
        path = tmp / "policy.json"
        path.write_text(json.dumps({"timezone": "Asia/Singapore"}), encoding="utf-8")
        return path

    def _write_config(self, tmp: Path, config: dict) -> Path:
        path = tmp / "user_config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_omitted_legacy_scope_keeps_discovery_in_reminders(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            policy_path = self._policy_file(tmp)
            config, report = uc.migrate_legacy({"timezone": "Asia/Singapore"})
            self.assertEqual(sorted(config["advanced"].get("legacy_account_discovery", [])),
                             ["calendar", "tasks"])
            self._write_config(tmp, config)
            policy, aliases, err = R.load_effective_policy(policy_path, require_config=True)
            self.assertEqual(err, "")
            self.assertIsNone(aliases, "library discovery must be preserved")
            # Monitoring must reach the same conclusion: the narrowing key is
            # never emitted for a discovery kind.
            self.assertNotIn("calendar_accounts", policy)
            self.assertNotIn("task_accounts", policy)

    def test_explicit_empty_accounts_still_deny(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            policy_path = self._policy_file(tmp)
            config = uc.validate_config(uc.default_config())
            self.assertEqual(config["advanced"].get("legacy_account_discovery"), None)
            self._write_config(tmp, config)
            _policy, aliases, err = R.load_effective_policy(policy_path, require_config=True)
            self.assertEqual(err, "")
            self.assertEqual(aliases, [], "an explicit empty scope must still deny")

    def test_explicit_accounts_are_honoured(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            policy_path = self._policy_file(tmp)
            config = uc.default_config()
            config["calendar"]["accounts"] = ["primary"]
            config["sources"]["calendar"] = True
            config = uc.validate_config(config)
            self._write_config(tmp, config)
            _policy, aliases, err = R.load_effective_policy(policy_path, require_config=True)
            self.assertEqual(err, "")
            self.assertEqual(aliases, ["primary"])


if __name__ == "__main__":
    unittest.main()
