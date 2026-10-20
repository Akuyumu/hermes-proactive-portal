"""Adversarial-query rejection regression tests.

The corpus is shipped fixture data used for detection tests only; it is never
executed. Every entry must still be rejected by ``validate_query``.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.planner import PlannerError, validate_query  # noqa: E402

CORPUS = ROOT / "fixtures" / "self_test_corpus.json"


class AdversarialCorpusTest(unittest.TestCase):
    def test_dangerous_queries_are_rejected(self):
        payload = json.loads(CORPUS.read_text(encoding="utf-8"))
        queries = payload["dangerous_queries"]
        self.assertTrue(queries)
        print("\ninput -> verdict")
        for query in queries:
            with self.subTest(query=query):
                try:
                    validate_query(query)
                except PlannerError:
                    print(f"  {query!r} -> rejected")
                    continue
                self.fail(f"dangerous query was accepted: {query!r}")


if __name__ == "__main__":
    unittest.main()
