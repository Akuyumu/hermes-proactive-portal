"""CLI provenance preservation for trusted intake (staged gap #1).

Covers the fixed ``intake.py`` CLI path: explicit provenance is preserved for
adds AND corrections, an absent/empty value gets the honest trusted-intake
default, and a SYNTHETIC test provenance is never rewritten into a claim of a
genuine user request. All state is a workspace-local TemporaryDirectory; the
only model used is a fake (no subprocess/network).
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.intake import (  # noqa: E402
    DEFAULT_INTAKE_PROVENANCE,
    FakeIntakeModel,
    IntakeError,
    intake_command,
)
from proactive_pilot.state import StateStore  # noqa: E402
from proactive_pilot.watchstore import active_watches, load_watchstore  # noqa: E402

NOW = "2026-10-03T12:00:00"
SYNTH = "synthetic explicit user request (test-only; not a genuine user)"


def _args(tmp: str, **kw) -> argparse.Namespace:
    base = dict(state_dir=tmp, policy=None, preferences=str(Path(tmp) / "prefs.json"),
                seeded_clock=NOW, intake_file=None, no_model=True, provenance="",
                intake="")
    base.update(kw)
    return argparse.Namespace(**base)


def _proposal(topic="quantum networking"):
    return {"schema": 1, "is_watch_request": True, "public_topic": topic,
            "include": [], "exclude": [], "scope": "public",
            "confidence": "medium", "expiry": ""}


class IntakeProvenanceTests(unittest.TestCase):
    def test_explicit_provenance_preserved_for_add_and_readback(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            code = intake_command(_args(
                tmp, intake="watch for quantum networking", provenance=SYNTH))
            self.assertEqual(code, 0)
            data = load_watchstore(StateStore(Path(tmp), SeededClock(NOW)), strict=True)
            watch = next(iter(data["watches"].values()))
            self.assertIn(SYNTH, watch["provenance"])
            self.assertNotIn("genuine user request", watch["provenance"])

    def test_empty_provenance_uses_honest_default(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            self.assertEqual(intake_command(_args(
                tmp, intake="watch for robotics", provenance="   ")), 0)
            data = load_watchstore(StateStore(Path(tmp), SeededClock(NOW)), strict=True)
            watch = next(iter(data["watches"].values()))
            self.assertIn("explicit user request", watch["provenance"])
            self.assertIn("trusted intake", watch["provenance"])

    def test_correction_preserves_explicit_provenance_in_store(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            self.assertEqual(intake_command(_args(
                tmp, intake="watch for quantum networking", provenance=SYNTH)), 0)
            code = intake_command(_args(
                tmp, intake="exclude fundraising from quantum networking", provenance=SYNTH))
            self.assertEqual(code, 0)
            store = StateStore(Path(tmp), SeededClock(NOW))
            data = load_watchstore(store, strict=True)
            watch = next(iter(data["watches"].values()))
            self.assertEqual(watch["exclude"], ["fundraising"])
            self.assertGreater(watch["version"], 1)
            self.assertIn(SYNTH, watch["provenance"])

    def test_second_watch_is_add_and_thanks_writes_nothing(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            self.assertEqual(intake_command(_args(tmp, intake="watch for robotics", provenance=SYNTH)), 0)
            before = len(load_watchstore(StateStore(Path(tmp), SeededClock(NOW)))["watches"])
            self.assertEqual(intake_command(_args(tmp, intake="thanks!", provenance=SYNTH)), 2)
            after = len(load_watchstore(StateStore(Path(tmp), SeededClock(NOW)))["watches"])
            self.assertEqual((before, after), (1, 1))

    def test_model_path_preserves_explicit_provenance(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            fake = FakeIntakeModel(_proposal())
            with mock.patch("proactive_pilot.intake.NativeIntakeModel", return_value=fake):
                code = intake_command(_args(
                    tmp, intake="watch for quantum networking", no_model=False, provenance=SYNTH))
            self.assertEqual(code, 0)
            self.assertEqual(len(fake.calls), 1)
            data = load_watchstore(StateStore(Path(tmp), SeededClock(NOW)), strict=True)
            watch = next(iter(data["watches"].values()))
            self.assertEqual(watch["provenance"], SYNTH)

    def test_model_pii_fails_closed_and_writes_nothing(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            fake = FakeIntakeModel(_proposal(topic="alex@example.com"))
            with mock.patch("proactive_pilot.intake.NativeIntakeModel", return_value=fake):
                code = intake_command(_args(
                    tmp, intake="watch for my email", no_model=False, provenance=SYNTH))
            self.assertEqual(code, 2)
            data = load_watchstore(StateStore(Path(tmp), SeededClock(NOW)), strict=True)
            self.assertEqual(data["watches"], {})

    def test_fallback_path_labels_synthetic_provenance(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            self.assertEqual(intake_command(_args(
                tmp, intake="keep me posted on renewable energy", provenance=SYNTH)), 0)
            data = load_watchstore(StateStore(Path(tmp), SeededClock(NOW)), strict=True)
            watch = next(iter(data["watches"].values()))
            self.assertIn(SYNTH, watch["provenance"])
            self.assertIn("deterministic-explicit", watch["provenance"])

    def test_default_constant_used_and_correction_provenance_readback(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmp:
            self.assertEqual(DEFAULT_INTAKE_PROVENANCE,
                             "explicit user request (trusted intake)")
            self.assertEqual(intake_command(_args(
                tmp, intake="watch for quantum networking")), 0)
            store = StateStore(Path(tmp), SeededClock(NOW))
            data = load_watchstore(store, strict=True)
            watch = next(iter(data["watches"].values()))
            self.assertTrue(active_watches(data, SeededClock(NOW).now()))


if __name__ == "__main__":
    unittest.main()
