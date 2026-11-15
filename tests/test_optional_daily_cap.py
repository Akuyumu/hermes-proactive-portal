"""Optional daily message ceiling: unset means no deterministic daily cap.

Offline regression coverage for the single validated owner: normalization,
partial-edit preservation vs explicit clearing, legacy migration, the native
form projection, the policy overlay, the evaluator packet and the delivery gate.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import userconfig as uc  # noqa: E402
from proactive_pilot import policy as policy_mod  # noqa: E402
from proactive_pilot import gates  # noqa: E402
from proactive_pilot.candidates import build_packet  # noqa: E402


class _Ctx:
    """Mirror of the host per-key native settings seam."""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.writes: list[tuple[str, object]] = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.writes.append((key, value))
        self.settings[key] = value


class _Clock:
    def __init__(self, now):
        self._now = now

    def now(self):
        return self._now


NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def _file_payload(**notification):
    return {
        "schema": 1,
        "version": 1,
        "notification": dict(notification),
    }


class DefaultAndNormalizationTests(unittest.TestCase):
    def test_fresh_default_has_no_hidden_five(self):
        cfg = uc.default_config()
        self.assertIsNone(cfg["notification"]["daily_normal_cap"])
        self.assertEqual(uc.DEFAULT_DAILY_NORMAL_CAP, None)
        self.assertIsNone(policy_mod.DEFAULT_POLICY["daily_normal_cap"])

    def test_validate_preserves_explicit_integers_including_zero(self):
        for value in (0, 1, 5, 250):
            cfg = uc.validate_config(
                _file_payload(daily_normal_cap=value))
            self.assertEqual(cfg["notification"]["daily_normal_cap"], value)

    def test_validate_blank_and_null_are_unset(self):
        for value in (None, "", "   "):
            cfg = uc.validate_config(
                _file_payload(daily_normal_cap=value))
            self.assertIsNone(cfg["notification"]["daily_normal_cap"])

    def test_validate_accepts_optional_numeric_text(self):
        cfg = uc.validate_config(_file_payload(daily_normal_cap="12"))
        self.assertEqual(cfg["notification"]["daily_normal_cap"], 12)

    def test_validate_rejects_bool_negative_fraction_and_text(self):
        for bad in (True, False, -1, 2.5, "2.5", "many", [5], {"n": 5}):
            with self.assertRaises(uc.ConfigError, msg=repr(bad)):
                uc.validate_config(_file_payload(daily_normal_cap=bad))


class PartialEditTests(unittest.TestCase):
    def test_omitted_field_preserves_previous_cap(self):
        base = _file_payload(daily_normal_cap=9)
        loaded = uc.validate_config(base)
        merged = uc._merge_flat_payload(base, {"timezone": "UTC"})
        self.assertEqual(
            uc.validate_config(merged)["notification"]["daily_normal_cap"], 9)
        self.assertEqual(loaded["notification"]["daily_normal_cap"], 9)

    def test_public_load_blank_form_value_clears_saved_cap(self):
        base = _file_payload(daily_normal_cap=9, quiet_start_hour=3,
                             persona="Keeper")
        base["timezone"] = "Europe/London"
        for native in (
                {"daily_normal_cap": ""},
                {"daily_normal_cap": "   "},
                {"daily_normal_cap": None},
                _Ctx({"daily_normal_cap": ""}),
                _Ctx({"daily_normal_cap": "   "}),
                _Ctx({"daily_normal_cap": None})):
            with self.subTest(native=native), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "user_config.json"
                path.write_text(json.dumps(base), encoding="utf-8")
                loaded = uc.load_user_config(path, native=native)
                self.assertIsNone(
                    loaded.config["notification"]["daily_normal_cap"])
                self.assertIsNone(
                    uc.policy_overrides(loaded.config)["daily_normal_cap"])
                # A blank cap edit must not disturb unrelated saved fields.
                self.assertEqual(loaded.config["timezone"], "Europe/London")
                self.assertEqual(
                    loaded.config["notification"]["quiet_start_hour"], 3)
                self.assertEqual(
                    loaded.config["notification"]["persona"], "Keeper")

    def test_public_load_omitted_native_cap_preserves_saved_cap(self):
        base = _file_payload(daily_normal_cap=9, quiet_start_hour=3,
                             persona="Keeper")
        base["timezone"] = "Europe/London"
        for native in ({"timezone": "UTC"}, _Ctx({"timezone": "UTC"})):
            with self.subTest(native=native), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "user_config.json"
                path.write_text(json.dumps(base), encoding="utf-8")
                loaded = uc.load_user_config(path, native=native)
                self.assertEqual(
                    loaded.config["notification"]["daily_normal_cap"], 9)
                self.assertEqual(
                    uc.policy_overrides(loaded.config)["daily_normal_cap"], 9)
                self.assertEqual(loaded.config["timezone"], "UTC")
                # The partial native edit leaves unrelated saved fields intact.
                self.assertEqual(
                    loaded.config["notification"]["quiet_start_hour"], 3)
                self.assertEqual(
                    loaded.config["notification"]["persona"], "Keeper")

    def test_invalid_partial_edit_is_rejected_not_defaulted(self):
        for bad in (-1, True, "2.5", "abc"):
            with self.assertRaises(uc.ConfigError, msg=repr(bad)):
                uc.load_user_config(native={"daily_normal_cap": bad})


class LegacyAndOverlayTests(unittest.TestCase):
    def test_explicit_legacy_values_survive_migration(self):
        for cap in (0, 3, 5):
            config, _report = uc.migrate_legacy(
                {"timezone": "UTC", "daily_normal_cap": cap})
            self.assertEqual(
                config["notification"]["daily_normal_cap"], cap)
            self.assertEqual(
                uc.policy_overrides(config)["daily_normal_cap"], cap)

    def test_omitted_typed_field_preserves_explicit_legacy_cap(self):
        raw = {"schema": 1, "version": 1,
               "advanced": {"legacy_policy": {"daily_normal_cap": 5}}}
        cfg = uc.validate_config(raw)
        self.assertIsNone(cfg["notification"]["daily_normal_cap"])
        self.assertEqual(uc.policy_overrides(cfg)["daily_normal_cap"], 5)

    def test_explicit_null_typed_field_clears_legacy_cap(self):
        raw = {"schema": 1, "version": 1,
               "notification": {"daily_normal_cap": None},
               "advanced": {"legacy_policy": {"daily_normal_cap": 5}}}
        cfg = uc.validate_config(raw)
        self.assertIsNone(uc.policy_overrides(cfg)["daily_normal_cap"])

    def test_policy_helper_blank_unset_unlimited_invalid_raises(self):
        for raw in (None, "", "   "):
            self.assertIsNone(
                policy_mod.daily_normal_cap({"daily_normal_cap": raw}))
        for raw in ("abc", "1.5", True, False, -2, -0.5, 1.5, [], {}):
            with self.assertRaises(ValueError):
                policy_mod.daily_normal_cap({"daily_normal_cap": raw})
        self.assertEqual(
            policy_mod.daily_normal_cap({"daily_normal_cap": "4"}), 4)
        self.assertEqual(
            policy_mod.daily_normal_cap({"daily_normal_cap": 0}), 0)


class ProjectionTests(unittest.TestCase):
    def test_native_projection_blank_when_unset_and_int_when_set(self):
        run = uc.validate_config(_file_payload(daily_normal_cap=7))
        self.assertEqual(uc.native_flat_values(run)["daily_normal_cap"], 7)
        unset = uc.validate_config(_file_payload(daily_normal_cap=None))
        self.assertEqual(uc.native_flat_values(unset)["daily_normal_cap"], "")

    def test_project_writes_blank_to_clear_but_preserves_omitted(self):
        ctx = _Ctx({"daily_normal_cap": 5})
        config = uc.validate_config(_file_payload(daily_normal_cap=None))
        written = uc.project_native_settings(ctx, config)
        self.assertIn("daily_normal_cap", written)
        self.assertEqual(ctx.settings["daily_normal_cap"], "")

    def test_manifest_declares_optional_supported_text_field(self):
        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        before, after = text.split("daily_normal_cap:", 1)
        field_line = before.rsplit("\n", 1)[-1]
        field_indent = len(field_line) - len(field_line.lstrip())
        block_lines = []
        for line in after.splitlines()[1:]:
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= field_indent:
                break
            block_lines.append(line)
        block = "\n".join(block_lines)
        self.assertIn("type: str", block)
        self.assertIn('default: ""', block)
        self.assertNotIn("type: int", block)


class GateAndPacketTests(unittest.TestCase):
    def _policy(self, cap):
        return {"timezone": "UTC", "quiet_start_hour": 0, "quiet_end_hour": 0,
                "cooldown_minutes": 0, "daily_normal_cap": cap}

    def _state(self, count):
        return {"delivered": [{"at": NOW.isoformat(), "signature": str(i)}
                              for i in range(count)]}

    def test_unset_cap_never_blocks_ordinary_messages(self):
        self.assertFalse(
            gates._over_cap(self._state(500), self._policy(None), NOW, "normal"))
        self.assertFalse(
            gates._over_cap(self._state(500), self._policy(""), NOW, "normal"))

    def test_explicit_zero_keeps_existing_blocking_semantics(self):
        self.assertTrue(
            gates._over_cap(self._state(0), self._policy(0), NOW, "normal"))

    def test_explicit_ceiling_and_urgent_exception(self):
        self.assertFalse(
            gates._over_cap(self._state(4), self._policy(5), NOW, "normal"))
        self.assertTrue(
            gates._over_cap(self._state(5), self._policy(5), NOW, "normal"))
        self.assertFalse(
            gates._over_cap(self._state(500), self._policy(5), NOW, "urgent"))

    def test_notice_allowed_uses_same_optional_cap(self):
        allowed, reason = gates.notice_allowed(
            self._state(50), self._policy(None), {}, NOW)
        self.assertTrue(allowed, reason)
        allowed, reason = gates.notice_allowed(
            self._state(50), self._policy(5), {}, NOW)
        self.assertFalse(allowed)
        self.assertEqual(reason, "daily cap")

    def test_packet_serialises_none_and_explicit_values(self):
        packet = build_packet([], clock=_Clock(NOW), policy=self._policy(None),
                              baseline={})
        self.assertIsNone(packet["daily_normal_cap"])
        packet = build_packet([], clock=_Clock(NOW), policy=self._policy(3),
                              baseline={})
        self.assertEqual(packet["daily_normal_cap"], 3)


if __name__ == "__main__":
    unittest.main()
