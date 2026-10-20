"""Responsibility lifecycle: accepted vs tentative, atomic claims, fencing, evidence."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot import responsibilities as R  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

T0 = datetime.fromisoformat("2026-10-20T10:00:00+08:00")


def prov(kind="user_commitment", ref="conversation:turn-7"):
    return {"kind": kind, "source_ref": ref, "conversation_ref": "chat:main", "at": T0.isoformat()}


def facts():
    return [{"ref": "mail:msg-1", "text": "Application closes on 2026-10-25.",
             "provenance": "authorized mail", "observed_at": T0.isoformat()}]


class ResponsibilityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or tempfile.gettempdir())
        self.root = Path(self._tmp.name)
        self.store = StateStore(self.root / "state", SeededClock(T0.isoformat()))
        self.addCleanup(self._tmp.cleanup)

    def create(self, **kw):
        data = dict(desired_outcome="Prepare the application checklist",
                    acceptance_criterion="Checklist covers every requirement with refs",
                    provenance=prov(),
                    due_at=(T0 - timedelta(minutes=1)).isoformat(),
                    evidence_refs=["mail:msg-1"],
                    completion_evidence_refs=["calendar:event-9"],
                    facts=facts())
        data.update(kw)
        return R.create(self.store, **data)

    def test_accepted_requires_user_commitment(self):
        with self.assertRaises(R.ResponsibilityError):
            self.create(provenance=prov("opportunity", "feed:item-1"))
        rec = self.create()
        self.assertEqual(rec["status"], R.ACCEPTED)
        self.assertEqual(rec["generation"], 1)

    def test_tentative_never_due_or_claimed(self):
        rec = self.create(status=R.TENTATIVE, provenance=prov("opportunity", "feed:item-1"))
        self.assertEqual(rec["status"], R.TENTATIVE)
        self.assertEqual(R.due_records(self.store, now=T0), [])
        self.assertIsNone(R.claim_due(self.store, now=T0))

    def test_due_claim_is_atomic_and_busy_claim_returns_none(self):
        rec = self.create()
        claim = R.claim_due(self.store, now=T0)
        self.assertIsNotNone(claim)
        self.assertEqual(claim["id"], rec["id"])
        self.assertIsNone(R.claim_due(self.store, now=T0))
        read = R.inspect(self.store, rec["id"], include_claim=True)
        self.assertEqual(read["claim"]["token"], claim["claim_token"])

    def test_expired_claim_recovers_then_can_be_reclaimed(self):
        rec = self.create()
        first = R.claim_due(self.store, now=T0, lease_minutes=1)
        later = T0 + timedelta(minutes=2)
        recovered = R.recover_expired(self.store, now=later)
        self.assertEqual(recovered, [rec["id"]])
        second = R.claim_due(self.store, now=later)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["claim_token"], second["claim_token"])

    def test_cancel_or_update_fences_stale_completion(self):
        rec = self.create()
        claim = R.claim_due(self.store, now=T0)
        R.cancel(self.store, rec["id"], now=T0)
        with self.assertRaises(R.ResponsibilityError):
            R.complete(self.store, rec["id"], claim_token=claim["claim_token"],
                       generation=claim["generation"],
                       source_version=claim["source_version"],
                       completion_evidence=["mail:msg-1"], now=T0)

        rec2 = self.create(desired_outcome="Second unit")
        claim2 = R.claim_due(self.store, now=T0)
        R.update(self.store, rec2["id"], source_version="v2", now=T0)
        with self.assertRaises(R.ResponsibilityError):
            R.checkpoint(self.store, rec2["id"], claim_token=claim2["claim_token"],
                         generation=claim2["generation"],
                         source_version=claim2["source_version"], now=T0)

    def test_completion_requires_separately_supplied_completion_evidence(self):
        rec = self.create()
        claim = R.claim_due(self.store, now=T0)
        # Original commitment provenance, initial evidence/facts and invented
        # refs are never completion proof.
        for evidence in (["conversation:turn-7"], ["mail:msg-1"], ["mail:invented"]):
            with self.assertRaises(R.ResponsibilityError):
                R.complete(self.store, rec["id"], claim_token=claim["claim_token"],
                           generation=claim["generation"],
                           source_version=claim["source_version"],
                           completion_evidence=evidence, now=T0)
        done = R.complete(self.store, rec["id"], claim_token=claim["claim_token"],
                          generation=claim["generation"],
                          source_version=claim["source_version"],
                          completion_evidence=["calendar:event-9"], now=T0)
        self.assertEqual(done["status"], R.COMPLETED)
        self.assertEqual(done["completion"]["evidence_refs"], ["calendar:event-9"])

    def test_checkpoint_keeps_claim_generation(self):
        rec = self.create()
        claim = R.claim_due(self.store, now=T0)
        out = R.checkpoint(self.store, rec["id"], claim_token=claim["claim_token"],
                           generation=claim["generation"],
                           source_version=claim["source_version"],
                           summary="half done", now=T0)
        self.assertEqual(out["generation"], claim["generation"])
        self.assertEqual(out["checkpoint"]["summary"], "half done")

    def test_concurrent_claim_only_one_winner(self):
        self.create()
        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            try:
                results.append(R.claim_due(self.store, now=T0))
            except R.ResponsibilityBusy:
                results.append(None)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wins = [r for r in results if r is not None]
        self.assertEqual(len(wins), 1, results)

    def test_invalid_inputs_fail_closed(self):
        with self.assertRaises(R.ResponsibilityError):
            self.create(due_at="2026-10-20T10:00:00")
        with self.assertRaises(R.ResponsibilityError):
            self.create(desired_outcome="x" * (R.MAX_DESIRED + 1))
        with self.assertRaises(R.ResponsibilityError):
            self.create(facts=[
                {"ref": f"mail:{i}", "text": "x", "provenance": "y"}
                for i in range(R.MAX_FACTS + 1)
            ])
        with self.assertRaises(R.ResponsibilityError):
            self.create(evidence_refs=["api_key: deadbeef"])

    def test_exact_readback(self):
        rec = self.create(external_refs=[{"kind": "calendar", "id": "evt:1", "version": "v1"}])
        read = R.inspect(self.store, rec["id"])
        self.assertEqual(read["external_refs"][0], {"kind": "calendar", "id": "evt:1", "version": "v1"})
        self.assertEqual(read["desired_outcome"], rec["desired_outcome"])
        self.assertEqual(read["acceptance_criterion"], rec["acceptance_criterion"])

    def test_shallow_or_missing_fields_fail_as_corrupt_not_keyerror(self):
        rec = self.create()
        data = self.store.load(R.STORE, {}, strict=True)
        del data["records"][rec["id"]]["desired_outcome"]
        self.store.save(R.STORE, data)
        with self.assertRaises(Exception) as caught:
            R.inspect(self.store, rec["id"])
        self.assertEqual(type(caught.exception).__name__, "CorruptStateError")
        self.assertNotIn("Prepare the application", str(caught.exception))

        bad = {"schema": R.SCHEMA, "version": 1, "records": {
            "resp_bad": {"id": "resp_bad", "status": "accepted", "generation": 1,
                         "claim": {}, "provenance": {"kind": "user_commitment"}}}}
        self.store.save(R.STORE, bad)
        with self.assertRaises(Exception) as caught:
            R.list_records(self.store)
        self.assertEqual(type(caught.exception).__name__, "CorruptStateError")

    def test_credential_filter_is_high_confidence_not_word_based(self):
        rec = self.create(facts=[{
            "ref": "mail:reset",
            "text": "Reset instructions: https://example.test/password-reset (password required).",
            "provenance": "authorized mail", "observed_at": T0.isoformat()}])
        self.assertIn("password-reset", rec["facts"][0]["text"])
        for secret in ("api_key=sk-live-0123456789abcdef",
                       "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
                       "-----BEGIN RSA PRIVATE KEY-----"):
            with self.assertRaises(R.ResponsibilityError):
                self.create(facts=[{"ref": "mail:secret", "text": f"value {secret}",
                                    "provenance": "mail"}])

    def test_capacity_requires_explicit_export_or_clear(self):
        records = []
        for index in range(R.MAX_RECORDS):
            records.append(self.create(
                desired_outcome=f"Unit {index}",
                responsibility_id=f"resp_capacity_{index:03d}"))
        status = R.capacity_status(self.store)
        self.assertTrue(status["full"])
        self.assertEqual(status["used"], R.MAX_RECORDS)
        with self.assertRaises(R.ResponsibilityError) as caught:
            self.create(desired_outcome="One too many",
                        responsibility_id="resp_capacity_over")
        self.assertIn("export", str(caught.exception))
        self.assertEqual(len(R.export_records(self.store)["records"]), R.MAX_RECORDS)
        # No automatic history deletion: nothing is removed without an explicit selector.
        with self.assertRaises(R.ResponsibilityError):
            R.clear_terminal(self.store)
        R.cancel(self.store, records[0]["id"], now=T0)
        removed = R.clear_terminal(self.store, responsibility_ids=[records[0]["id"]],
                                   now=T0)
        self.assertEqual(removed, [records[0]["id"]])
        self.create(desired_outcome="After explicit clear",
                    responsibility_id="resp_capacity_after")


if __name__ == "__main__":
    unittest.main()
