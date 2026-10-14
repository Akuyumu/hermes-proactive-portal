"""Actual scheduled path, real collector routing, synthetic provider transports.

All artifacts stay in candidate temporary directories; stdout is captured and
no native provider, network, scheduler or delivery transport is used.
"""
import argparse
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from proactive_pilot import main, source_health as sh
from proactive_pilot.clock import SeededClock
from proactive_pilot.collectors import SourceResult
from proactive_pilot.evaluator import EvaluatorResult, FakeEvaluator
from proactive_pilot.feedback import empty_preferences
from proactive_pilot.policy import DEFAULT_POLICY
from proactive_pilot.state import StateStore
from proactive_pilot import publicfeeds as pf

ROOT = Path(__file__).resolve().parents[1]


class Provider:
    def __init__(self):
        self.calls = []
        self.error = "401 unauthorized"
        self.rows = []
        self.peer_rows = []

    def run(self, argv, timeout):
        self.calls.append(list(argv))
        account = argv[argv.index("-a") + 1]
        if account == "critical" and self.error:
            return 1, "", self.error
        if "gmail" in argv:
            return 0, json.dumps({"ids": [], "next_page": None}), ""
        return 0, json.dumps({"envelopes": self.rows if account == "critical" else self.peer_rows}), ""

    def count(self, account="critical", native=False):
        return sum(c[c.index("-a") + 1] == account and (("gmail" in c) == native)
                   for c in self.calls)


class ScheduledHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policy = deepcopy(DEFAULT_POLICY)
        self.policy.update(mail_accounts=["critical", "peer"], calendar_enabled=False,
                           tasks_enabled=False, feeds_enabled=False, obligations_enabled=False,
                           productivity_secretary_enabled=False, mail_preview_items=0,
                           health_critical_sources=["mail:critical"],
                           quiet_start_hour=0, quiet_end_hour=0, cooldown_minutes=0)
        self.provider = Provider()
        self.prefs = empty_preferences()

    def run_at(self, hour, minute=0, *, evaluator=None, save=None, emit=None, public_fetcher=None):
        at = f"2026-10-03T{hour:02}:{minute:02}:00+08:00"
        path = self.root / "policy.json"
        path.write_text(json.dumps(self.policy))
        args = argparse.Namespace(command="run", seeded_clock=at, policy=str(path),
                                  preferences=str(self.root / "preferences.json"),
                                  state_dir=str(self.root / "state"), fixture=None,
                                  check=False, collect_only=False, dry_run=False,
                                  canary=False, no_model=True, requested_reminder=False,
                                  public_fetcher=public_fetcher)
        out, err = io.StringIO(), io.StringIO()
        with patch("proactive_pilot.collectors.SubprocessRunner", return_value=self.provider), \
                patch.object(main, "load_preferences", return_value=self.prefs), \
                redirect_stdout(out), redirect_stderr(err):
            with patch.object(StateStore, "save", autospec=True, side_effect=save) if save else _null():
                with patch.object(main, "_emit", side_effect=emit) if emit else _null():
                    rc = main.run_once(args, evaluator=evaluator or FakeEvaluator(EvaluatorResult("silence")))
        return rc, out.getvalue(), err.getvalue()

    def state(self):
        return json.loads((self.root / "state" / "notify_state.json").read_text())

    def seed(self, error="401 unauthorized", end=13):
        self.provider.error = error
        for hour in range(10, end + 1):
            rc, output, _ = self.run_at(hour)
            self.assertEqual(rc, 0)
            self.assertEqual(output, "")

    def test_scoped_actual_requests_gate_restart_one_probe(self):
        self.seed()
        self.assertEqual(self.provider.count(), 4)
        rec = self.state()["source_health"]["sources"]["mail:critical"]
        self.assertEqual(rec["recovery_attempts"], 1)
        self.assertEqual(rec["last_recovery_outcome"], "failure")
        rc, out, _ = self.run_at(13, 1)
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.provider.count(), 4)
        self.assertEqual(self.provider.count("peer"), 5)
        self.assertEqual(self.state()["source_health"]["sources"]["mail:critical"]["attempted_checks"], 4)
        rc, out, _ = self.run_at(14)
        self.assertEqual(rc, 0)
        self.assertIn("credentials", out)
        self.assertEqual(self.provider.count(), 5)
        record = self.state()["source_health"]["sources"]["mail:critical"]
        self.assertEqual(record["recovery_attempts"], 2)
        self.assertEqual(record["delivery_state"], "emitted")
        signature = self.state()["delivered"][-1]["signature"]
        self.assertTrue(signature.startswith("source-health:"))
        self.assertEqual(self.run_at(14, 1)[1], "")
        self.assertEqual(self.run_at(15)[1], "")
        self.assertEqual(self.state()["source_health"]["sources"]["mail:critical"]["recovery_attempts"], 2)

    def test_single_429_blocks_before_threshold_actual_transport(self):
        self.provider.error = "429 Retry-After: 7200"
        self.run_at(10)
        self.run_at(11)
        self.assertEqual(self.provider.count(), 1)
        self.run_at(12)
        self.assertEqual(self.provider.count(), 2)
        self.run_at(12, 1)
        self.assertEqual(self.provider.count(), 2)
        self.assertEqual(self.provider.count("peer"), 4)

    def test_overload_connectivity_permission_noncritical_matrix(self):
        for error in ["HTTP 503", "timeout", "connection failed", "403 permission denied"]:
            with self.subTest(error=error):
                self.provider.error = error
                for hour in range(10, 17):
                    self.assertEqual(self.run_at(hour)[1], "")
        self.policy["health_critical_sources"] = []
        self.provider.error = "401 unauthorized"
        for hour in range(17, 20):
            self.assertEqual(self.run_at(hour)[1], "")

    def test_authoritative_success_new_incident_partial_does_not_recover(self):
        self.seed()
        self.run_at(14)
        incident = self.state()["source_health"]["sources"]["mail:critical"]["incident_id"]
        self.provider.error = ""
        # Invalid date forces the real collector to report a partial read.
        self.provider.rows = [{"id": "synthetic", "subject": "Synthetic business",
                               "date": "invalid", "from": []}]
        self.assertEqual(self.run_at(15)[1], "")
        self.assertEqual(self.state()["source_health"]["sources"]["mail:critical"]["incident_id"], incident)
        self.provider.rows = []
        self.run_at(16)
        fresh = self.state()["source_health"]["sources"]["mail:critical"]
        self.assertNotEqual(fresh["incident_id"], incident)
        self.assertEqual(fresh["failure_streak"], 0)
        self.provider.error = "401 unauthorized"
        for hour in range(17, 21):
            self.assertEqual(self.run_at(hour)[1], "")
        self.assertIn("credentials", self.run_at(21)[1])

    def test_pause_quiet_snooze_cooldown_cap_not_consumed(self):
        self.seed()
        self.prefs["paused"] = {"active": True}
        before = len(self.provider.calls)
        self.assertEqual(self.run_at(14)[1], "")
        self.assertEqual(len(self.provider.calls), before)
        self.prefs = empty_preferences()
        self.policy.update(quiet_start_hour=13, quiet_end_hour=16)
        self.assertEqual(self.run_at(14)[1], "")
        self.assertEqual(self.state()["source_health"]["sources"]["mail:critical"]["alerted_incident"], 0)
        self.policy.update(quiet_start_hour=0, quiet_end_hour=0)
        self.prefs["snoozes"] = [{"until": "2026-10-03T16:00:00+08:00"}]
        self.assertEqual(self.run_at(15)[1], "")
        self.prefs = empty_preferences()
        data = self.state()
        data["last_delivery_at"] = "2026-10-03T15:59:00+08:00"
        (self.root / "state" / "notify_state.json").write_text(json.dumps(data))
        self.policy["cooldown_minutes"] = 30
        self.assertEqual(self.run_at(16)[1], "")
        self.policy["cooldown_minutes"] = 0
        self.policy["daily_normal_cap"] = 0
        self.assertEqual(self.run_at(17)[1], "")
        self.policy["daily_normal_cap"] = 10
        self.assertIn("credentials", self.run_at(18)[1])

    def test_save_failure_before_stdout_leaves_incident_unconsumed(self):
        self.seed()
        original = StateStore.save
        calls = 0
        def save(store, name, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic outbox failure")
            original(store, name, payload)
        rc, out, err = self.run_at(14, save=save)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("outbox-save-failed", err)
        self.assertEqual(self.state()["source_health"]["sources"]["mail:critical"]["alerted_incident"], 0)
        self.assertIn("credentials", self.run_at(15)[1])

    def test_save_failure_after_stdout_restart_no_duplicate(self):
        self.seed()
        original = StateStore.save
        calls = 0
        def save(store, name, payload):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("synthetic commit failure")
            original(store, name, payload)
        rc, out, _ = self.run_at(14, save=save)
        self.assertEqual(rc, 1)
        self.assertIn("credentials", out)
        self.assertEqual(self.state()["source_health"]["sources"]["mail:critical"]["delivery_state"], "emitting")
        rc, out, err = self.run_at(15)
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("delivery-uncertain", err)
        self.assertEqual(self.state()["source_health"]["sources"]["mail:critical"]["delivery_state"], "uncertain")

    def test_emission_exception_restart_no_blind_retry(self):
        self.seed()
        rc, out, _ = self.run_at(14, emit=OSError("synthetic stdout failure"))
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(self.run_at(15)[1], "")

    def test_obligation_actual_native_requests_independent_scope(self):
        self.policy.update(mail_enabled=False, obligations_enabled=True,
                           health_critical_sources=["obligation:critical"])
        for hour in range(10, 14):
            self.assertEqual(self.run_at(hour)[1], "")
        self.assertEqual(self.provider.count(native=True), 4)
        self.run_at(13, 1)
        self.assertEqual(self.provider.count(native=True), 4)
        self.assertEqual(self.provider.count("peer", native=True), 5)
        self.assertIn("credentials", self.run_at(14)[1])

    def test_runtime_negative_health_matrix_with_generic_business_words(self):
        self.provider.error = "HTTP 503"
        for minute, message in enumerate([
                "Coverage note: mail is unavailable; payment forms may be due.",
                "Provider timeout; the next health check is due.",
                "Unable to check calendar; appointments may be missed.",
                "Sources are having trouble; will keep retrying."]):
            proposal = EvaluatorResult("notify", kind="mail", key="missing", message=message)
            self.assertEqual(self.run_at(10, minute, evaluator=FakeEvaluator(proposal))[1], "")
        for minute in range(4, 9):
            self.assertEqual(self.run_at(10, minute, evaluator=FakeEvaluator(EvaluatorResult("error")))[1], "")

    def test_real_business_competes_without_consuming_health(self):
        self.seed()
        self.provider.peer_rows = [{"id": "invoice", "subject": "Acme renewal invoice",
                                    "date": "2026-10-03T13:55:00+08:00", "from": []}]
        proposal = EvaluatorResult("notify", kind="mail", key="mail:peer:invoice",
                                   message="Acme renewal invoice payment is due today; a provider timeout risks late payment.",
                                   evidence=["mail:peer:invoice"])
        rc, out, _ = self.run_at(14, evaluator=FakeEvaluator(proposal))
        self.assertEqual(rc, 0)
        self.assertIn("invoice", out)
        self.assertNotIn("Critical source", out)
        self.assertEqual(self.state()["source_health"]["sources"]["mail:critical"]["alerted_incident"], 0)
        self.provider.peer_rows = []
        self.assertIn("credentials", self.run_at(15)[1])

    def test_public_actual_fetch_429_and_independent_feed(self):
        self.policy.update(mail_enabled=False, feeds_enabled=True, feed_seed_sources=[],
                           feed_max_sources_per_run=2, health_critical_sources=[])
        now = SeededClock("2026-10-03T10:00:00+08:00").now()
        store = StateStore(self.root / "state", SeededClock(now.isoformat()))
        registry = pf.new_registry()
        urls = ["https://github.com/example/critical/releases.atom",
                "https://github.com/example/peer/releases.atom"]
        for url in urls:
            self.assertTrue(pf.registry_add(registry, kind="rss", url=url, provenance="synthetic",
                                           interest="synthetic", now=now, cadence_minutes=1))
        pf.save_registry(store, registry)
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            return pf.FetchResponse(429 if "critical" in url else 200,
                                    {"Retry-After": "7200"}, b"<rss><channel/></rss>", url)
        self.run_at(10, public_fetcher=fetch)
        self.run_at(11, public_fetcher=fetch)
        self.assertEqual(calls.count(urls[0]), 1)
        self.assertEqual(calls.count(urls[1]), 2)
        self.run_at(12, public_fetcher=fetch)
        self.assertEqual(calls.count(urls[0]), 2)
        self.assertEqual(calls.count(urls[1]), 3)

    def test_corrupt_persisted_state_denies_alert_until_actual_success(self):
        self.seed()
        data = self.state()
        data["source_health"]["sources"]["mail:critical"]["failure_streak"] = True
        (self.root / "state" / "notify_state.json").write_text(json.dumps(data))
        for hour in range(14, 19):
            self.assertEqual(self.run_at(hour)[1], "")
        self.assertTrue(self.state()["source_health"]["corrupt"])
        self.assertIn("authoritative", self.state()["source_health"]["diagnostic"])
        self.provider.error = ""
        self.run_at(19)
        self.provider.error = "401 unauthorized"
        for hour in range(20, 24):
            self.assertEqual(self.run_at(hour)[1], "")


class SchemaAndEvidenceTests(unittest.TestCase):
    def test_full_field_corruption_matrix_never_crashes_or_resets_alerting(self):
        now = SeededClock("2026-10-03T10:00:00+08:00").now()
        base = {}
        sh.record_observations(base, [SourceResult("mail:x", "error", error="401 unauthorized")], now, DEFAULT_POLICY)
        bad_values = {"incident_id": "1", "failure_streak": True, "attempted_checks": -1,
                      "recovery_attempts": 10**20, "next_permitted_at": [],
                      "last_success_at": "not-a-clock", "failure_class": "invented",
                      "recovery_state": "invented", "unresolved_alerted": 1,
                      "alerted_incident": None, "delivery_state": "sent-ish"}
        for field, value in bad_values.items():
            with self.subTest(field=field):
                state = deepcopy(base)
                state["source_health"]["sources"]["mail:x"][field] = value
                self.assertTrue(sh.dispatch_gate(state, "mail:x", now, DEFAULT_POLICY)["dispatch"])
                sh.record_observations(state, [SourceResult("mail:x", "error", error="401 unauthorized")], now, DEFAULT_POLICY)
                self.assertTrue(state["source_health"]["corrupt"])
                self.assertEqual(sh.alerting_sources(state, now + timedelta(days=1), DEFAULT_POLICY), [])

    def test_ok_unhealthy_and_skipped_not_recovery(self):
        for result in [SimpleNamespace(status="ok", healthy=False, name="x"),
                       SourceResult("x", "ok", attempted=False),
                       SourceResult("x", "skipped"), SourceResult("x", "ok", error="503")]:
            self.assertEqual(sh.classify_observation(result), sh.OBSERVATION_NON_EVIDENTIAL)

    def test_evidence_backed_business_warning_with_health_marker_allowed(self):
        candidate = {"kind": "obligation", "ref": "invoice:1", "subject": "Acme renewal invoice",
                     "date": "2026-10-03T18:00:00+08:00"}
        proposal = EvaluatorResult("notify", kind="obligation", key="invoice:1",
                                   message="Acme renewal invoice is due at 18:00; payment provider timeout risks a late fee.",
                                   evidence=["invoice:1"], consequence="late fee")
        self.assertIsNone(sh.routine_health_reason(proposal.message, proposal=proposal,
                                                  packet={"candidates": [candidate]}))
        proposal.message = "Provider timeout; forms and payment are due."
        self.assertIsNotNone(sh.routine_health_reason(proposal.message, proposal=proposal,
                                                     packet={"candidates": [candidate]}))


from contextlib import nullcontext as _null
