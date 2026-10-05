"""Trusted intake fake tests: explicit-only, strict validation, durability.

No subprocess, no network, no model: the intake model is faked.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.intake import (  # noqa: E402
    FakeIntakeModel,
    IntakeError,
    intake_command,
    intake_to_action,
    is_acknowledgement,
    looks_like_explicit_request,
)
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.watchstore import load_watchstore  # noqa: E402

NOW = SeededClock("2026-10-03T12:00:00").now()


def _policy():
    return json.loads(json.dumps(DEFAULT_POLICY))


def _proposal(topic="quantum networking", scope="public", include=None, exclude=None):
    return {"schema": 1, "is_watch_request": True, "public_topic": topic,
            "include": include or [], "exclude": exclude or [], "scope": scope,
            "confidence": "medium", "expiry": ""}


class TrustedIntakeTests(unittest.TestCase):
    def test_acknowledgements_never_create_a_watch(self):
        for text in ("thanks", "thank you so much", "ok", "read", "silence", "", "   "):
            self.assertTrue(is_acknowledgement(text), text)
            model = FakeIntakeModel(_proposal())
            with self.assertRaises(IntakeError):
                intake_to_action(text, policy=_policy(), model=model, now=NOW)
            self.assertEqual(model.calls, [], f"model must not run for {text!r}")

    def test_non_explicit_chat_rejected_before_model(self):
        model = FakeIntakeModel(_proposal())
        with self.assertRaises(IntakeError):
            intake_to_action("what is the weather today?", policy=_policy(), model=model, now=NOW)
        self.assertEqual(model.calls, [])

    def test_explicit_request_validated_from_model(self):
        model = FakeIntakeModel(_proposal(topic="quantum networking",
                                          include=["research"], exclude=["jobs"]))
        action = intake_to_action("please watch for quantum networking research, no jobs",
                                  policy=_policy(), model=model, now=NOW)
        self.assertEqual(action["action"], "add")
        self.assertEqual(action["public_topic"], "quantum networking")
        self.assertEqual(action["scope"], "public")
        self.assertEqual(action["include"], ["research"])
        self.assertEqual(action["exclude"], ["jobs"])
        self.assertIn("provenance", action)

    def test_model_proposing_pii_is_rejected_not_fallen_back(self):
        model = FakeIntakeModel(_proposal(topic="alex@example.com"))
        with self.assertRaises(IntakeError):
            intake_to_action("watch for my email", policy=_policy(), model=model, now=NOW)

    def test_model_proposing_url_in_criteria_rejected(self):
        model = FakeIntakeModel(_proposal(include=["https://evil.example/x"]))
        with self.assertRaises(IntakeError):
            intake_to_action("watch for stuff", policy=_policy(), model=model, now=NOW)

    def test_deterministic_fallback_from_explicit_request(self):
        self.assertTrue(looks_like_explicit_request("keep me posted on renewable energy"))
        action = intake_to_action("keep me posted on renewable energy",
                                  policy=_policy(), model=None, now=NOW)
        self.assertEqual(action["action"], "add")
        self.assertIn("renewable energy", action["public_topic"])
        self.assertIn("deterministic-explicit", action["provenance"])

    def test_fallback_rejects_pure_chat(self):
        with self.assertRaises(IntakeError):
            intake_to_action("just saying hi and talking about lunch",
                             policy=_policy(), model=None, now=NOW)

    def test_intake_cli_persists_durable_versioned_watch(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            args = argparse.Namespace(
                state_dir=tmp, policy=None, preferences=str(Path(tmp) / "prefs.json"),
                seeded_clock="2026-10-03T12:00:00", intake="watch for quantum networking",
                intake_file=None, no_model=True)
            code = intake_command(args)
            self.assertEqual(code, 0)
            store = StateStore(Path(tmp), SeededClock("2026-10-03T12:00:00"))
            data = load_watchstore(store, strict=True)
            self.assertEqual(len(data["watches"]), 1)
            watch = next(iter(data["watches"].values()))
            self.assertEqual(watch["version"], 1)
            self.assertEqual(watch["status"], "active")
            self.assertIn("explicit", watch["provenance"])

    def test_second_intake_is_a_new_watch_and_thanks_is_noop(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            base = dict(state_dir=tmp, policy=None,
                        preferences=str(Path(tmp) / "prefs.json"),
                        seeded_clock="2026-10-03T12:00:00", intake_file=None,
                        no_model=True)
            self.assertEqual(intake_command(argparse.Namespace(intake="watch for quantum", **base)), 0)
            self.assertEqual(intake_command(argparse.Namespace(intake="watch for robotics", **base)), 0)
            store = StateStore(Path(tmp), SeededClock("2026-10-03T12:00:00"))
            self.assertEqual(len(load_watchstore(store, strict=True)["watches"]), 2)
            # An acknowledgement is rejected and writes nothing.
            self.assertEqual(intake_command(argparse.Namespace(intake="thanks!", **base)), 2)
            self.assertEqual(len(load_watchstore(store, strict=True)["watches"]), 2)


if __name__ == "__main__":
    unittest.main()
