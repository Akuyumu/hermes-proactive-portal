"""Config-driven explicit account/source mapping in the collectors.

Legacy (no config) behaviour is preserved; an explicit configuration narrows or
widens access only as named, never from credential presence.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.collectors import (  # noqa: E402
    ALLOWED_MAIL_ACCOUNTS,
    collect_himalaya,
    configured_calendar_accounts,
    configured_mail_accounts,
    default_sources,
)


class _Clock:
    def __init__(self):
        from datetime import datetime, timezone
        self._now = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

    def now(self):
        return self._now


class _Runner:
    def run(self, argv, timeout):
        # Return an empty successful envelope list so no real CLI is invoked.
        return 0, "[]", ""


class AccountResolutionTests(unittest.TestCase):
    def test_default_preserves_legacy_accounts(self):
        self.assertEqual(configured_mail_accounts({}), ALLOWED_MAIL_ACCOUNTS)
        self.assertIsNone(configured_calendar_accounts({}))

    def test_explicit_empty_means_none(self):
        policy = {"mail_accounts": [], "calendar_accounts": []}
        self.assertEqual(configured_mail_accounts(policy), ())
        self.assertEqual(configured_calendar_accounts(policy), [])

    def test_explicit_unknown_account_is_supported(self):
        policy = {"mail_accounts": ["work", "school-2"]}
        self.assertEqual(configured_mail_accounts(policy), ("work", "school-2"))

    def test_collect_himalaya_rejects_unconfigured_account(self):
        result = collect_himalaya("custom", 5, clock=_Clock(), runner=_Runner(),
                                  allowed_accounts=("formal", "informal"))
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error, "account not authorized")

    def test_collect_himalaya_accepts_explicitly_configured_account(self):
        result = collect_himalaya("custom", 5, clock=_Clock(), runner=_Runner(),
                                  allowed_accounts=("custom",))
        self.assertNotEqual(result.error, "account not authorized")


class DefaultSourcesTests(unittest.TestCase):
    def _names(self, factories):
        return {f().name for f in factories}

    def test_legacy_policy_keeps_calendar_tasks_mail(self):
        names = self._names(default_sources({}, _Clock()))
        self.assertIn("mail:formal", names)
        self.assertIn("mail:informal", names)
        self.assertTrue(any(n.startswith("calendar:") for n in names))
        self.assertTrue(any(n.startswith("tasks:") for n in names))

    def test_mail_disabled_removes_mail_factories(self):
        names = self._names(default_sources({"mail_enabled": False}, _Clock()))
        self.assertFalse(any(n.startswith("mail:") for n in names))

    def test_configured_calendar_accounts_narrow_reads(self):
        policy = {"calendar_accounts": []}
        names = self._names(default_sources(policy, _Clock()))
        self.assertFalse(any(n.startswith("calendar:") for n in names))
        self.assertFalse(any(n.startswith("tasks:") for n in names))

    def test_configured_mail_accounts_replace_legacy(self):
        policy = {"mail_enabled": True, "mail_accounts": ["work"]}
        names = self._names(default_sources(policy, _Clock()))
        self.assertIn("mail:work", names)
        self.assertNotIn("mail:formal", names)

    def test_gws_calendar_factory_only_when_enabled_and_configured(self):
        policy = {"gws_calendar_enabled": True, "calendar_ids": ["primary"]}
        names = self._names(default_sources(policy, _Clock()))
        self.assertIn("calendar:gws:primary", names)
        # Disabled by config: no gws factory.
        names_off = self._names(default_sources({"calendar_ids": ["primary"]}, _Clock()))
        self.assertFalse(any(n.startswith("calendar:gws:") for n in names_off))


if __name__ == "__main__":
    unittest.main(verbosity=2)
