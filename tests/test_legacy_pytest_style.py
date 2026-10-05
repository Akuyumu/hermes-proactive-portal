"""load_tests adapter: collect legacy module-level ``test_*`` functions.

Many historical test modules in this suite are written pytest-style (module
level ``def test_...`` functions) and are therefore NOT collected by
``unittest discover``. This adapter walks every sibling ``test_*.py`` module and
wraps each zero-argument module-level ``test_*`` function in a
``unittest.FunctionTestCase`` so the parent's exact command
(``python3 -m unittest discover -s tests``) actually EXECUTES that behaviour.

Two honesty guards keep the reported count meaningful:

* Functions that a module has already dynamically attached to a local
  ``unittest.TestCase`` subclass (the ``_attach``/``AwarenessControls`` pattern)
  are NOT wrapped again -- otherwise the same test would run twice and inflate
  the reported total. They are recorded as ``duplicates_dropped``.
* Modules that cannot be imported are reported with their module name and reason
  in ``SKIPPED_IMPORTS`` instead of being silently hidden.

No new dependency is added, and only unique test ids are collected.
"""
from __future__ import annotations

import importlib
import inspect
import unittest
from pathlib import Path

_SELF = Path(__file__).name

# Report of what the adapter did, populated on each collection pass.
SKIPPED_IMPORTS: list[dict] = []
DUPLICATES_DROPPED: list[str] = []
COLLECTED_IDS: list[str] = []


def _attached_function_ids(module: object) -> set[int]:
    """Function objects a module attached onto a local TestCase subclass.

    The common pattern is
    ``setattr(cls, name, (lambda self, _fn=_fn: _fn()))``; the original function
    is captured in the lambda's defaults. A direct
    ``MyTest.test_x = test_x`` assignment is also recognised.
    """
    attached: set[int] = set()
    module_name = getattr(module, "__name__", "")
    for obj in vars(module).values():
        if not (inspect.isclass(obj) and issubclass(obj, unittest.TestCase)):
            continue
        if getattr(obj, "__module__", "") != module_name:
            continue
        for attr in vars(obj).values():
            if not inspect.isfunction(attr):
                continue
            attached.add(id(attr))
            for default in getattr(attr, "__defaults__", None) or ():
                if inspect.isfunction(default):
                    attached.add(id(default))
    return attached


def _collect_function_tests() -> unittest.TestSuite:
    suite = unittest.TestSuite()
    seen_ids: set[int] = set()
    here = Path(__file__).resolve().parent

    SKIPPED_IMPORTS.clear()
    DUPLICATES_DROPPED.clear()
    COLLECTED_IDS.clear()

    for path in sorted(here.glob("test_*.py")):
        if path.name == _SELF:
            continue
        modname = path.stem
        try:
            module = importlib.import_module(modname)
        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            SKIPPED_IMPORTS.append({
                "module": modname,
                "reason": f"{type(exc).__name__}: {exc}",
            })
            continue
        attached = _attached_function_ids(module)
        for name, obj in sorted(vars(module).items()):
            if not name.startswith("test_") or not inspect.isfunction(obj):
                continue
            if getattr(obj, "__module__", "") != module.__name__:
                continue
            if id(obj) in attached:
                # Already executed as a collected TestCase method.
                DUPLICATES_DROPPED.append(f"{modname}.{name}")
                continue
            if id(obj) in seen_ids:
                DUPLICATES_DROPPED.append(f"{modname}.{name}")
                continue
            try:
                params = inspect.signature(obj).parameters
            except (TypeError, ValueError):
                continue
            if len(params) != 0:
                continue
            seen_ids.add(id(obj))
            test_id = f"{modname}.{name}"
            COLLECTED_IDS.append(test_id)
            suite.addTest(unittest.FunctionTestCase(obj, description=test_id))
    return suite


def collection_report() -> dict:
    """Honest summary of the legacy function collection (for review/reporting)."""
    return {
        "collected": len(COLLECTED_IDS),
        "unique": len(set(COLLECTED_IDS)),
        "duplicates_dropped": list(DUPLICATES_DROPPED),
        "skipped_imports": list(SKIPPED_IMPORTS),
    }


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite,
               pattern: str | None) -> unittest.TestSuite:  # noqa: ARG001
    # Keep this module's own TestCase classes (the report controls) alongside
    # the legacy module-level function tests.
    suite = _collect_function_tests()
    suite.addTests(tests)
    return suite


class LegacyCollectionReportTests(unittest.TestCase):
    """The adapter must report honestly and never double-count a test."""

    def test_collected_ids_are_unique(self):
        _collect_function_tests()
        report = collection_report()
        self.assertEqual(report["collected"], len(COLLECTED_IDS))
        self.assertEqual(report["unique"], len(set(COLLECTED_IDS)))
        self.assertEqual(report["collected"], report["unique"],
                         "no duplicate test id may be collected")

    def test_duplicates_are_reported_not_collected(self):
        _collect_function_tests()
        dropped = set(DUPLICATES_DROPPED)
        self.assertTrue(dropped.isdisjoint(set(COLLECTED_IDS)),
                        "a dropped duplicate must not appear in the collected ids")

    def test_skipped_imports_carry_a_reason(self):
        _collect_function_tests()
        for entry in SKIPPED_IMPORTS:
            self.assertIn("module", entry)
            self.assertIn("reason", entry)
            self.assertTrue(str(entry["reason"]).strip(),
                            "a skipped import must state why, not hide coverage")
