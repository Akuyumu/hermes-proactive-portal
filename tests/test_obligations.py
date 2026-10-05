"""General-obligation follow-through tests: conservative extraction, marketing
false positives, no invented due dates, distinct orders, evidence-only
resolution, disappearance != satisfaction, retention across fresh processes,
undated no-urgency, shared cap, pause blocks reads, and no raw-body leakage.

Standalone (pytest not required):
  TMPDIR=<scratch> python3 tests/test_obligations.py
All mail fixtures are synthetic; no production account is read.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.candidates import build_packet, packet_digest, _time_phase  # noqa: E402
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import SourceResult  # noqa: E402
from proactive_pilot.evaluator import EvaluatorResult  # noqa: E402
from proactive_pilot.gates import decide  # noqa: E402
from proactive_pilot.obligations import (  # noqa: E402
    collect_obligations,
    extract_signals,
    health,
    load_obligations,
    merge_signals,
    new_store,
    object_key,
    prune,
    save_obligations,
)
from proactive_pilot.policy import load_policy  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=SG)


class MailRunner:
    """Fixed-argv fake: NATIVE Gmail id pages + metadata gets + read bodies.

    Emulates the supported provider-native route the obligations flow uses:
    ``gmail messages list`` returns ``{"ids":[{"id","threadId"}],"next_page"}``
    and ``gmail messages get --format metadata`` returns top-level ``headers``.
    Plain ``envelope`` calls (base-mail fallback) are also tolerated.
    """

    def __init__(self, account_envelopes, bodies=None):
        self.account_envelopes = account_envelopes
        self.bodies = bodies or {}
        self.calls: list[list[str]] = []

    @staticmethod
    def _is_native_list(argv):
        return "gmail" in argv and "list" in argv

    @staticmethod
    def _is_native_get(argv):
        return "gmail" in argv and "get" in argv

    def run(self, argv, timeout):
        self.calls.append(list(argv))
        if self._is_native_list(argv):
            account = argv[argv.index("-a") + 1]
            envs = self.account_envelopes.get(account, [])
            ids = [{"id": str(e["id"]), "threadId": str(e["id"])} for e in envs]
            return 0, json.dumps({"ids": ids, "next_page": None}), ""
        if self._is_native_get(argv):
            mid = argv[-1]
            env = next((e for e in self._all_envs() if str(e["id"]) == mid), {})
            frm = env.get("from") or [{}]
            sender = (frm[0] or {}).get("email") or (frm[0] or {}).get("name") or ""
            headers = [
                {"name": "From", "value": str(sender)},
                {"name": "Subject", "value": str(env.get("subject") or "")},
                {"name": "Date", "value": str(env.get("date") or "")},
            ]
            return 0, json.dumps({"id": mid, "headers": headers,
                                  "snippet": "DISCARDED"}), ""
        if "envelope" in argv:
            account = argv[argv.index("-a") + 1]
            page = int(argv[argv.index("--page") + 1])
            envs = self.account_envelopes.get(account, [])
            return 0, json.dumps({"envelopes": envs if page == 1 else []}), ""
        if "read" in argv:
            mid = argv[argv.index("read") + 1]
            return 0, self.bodies.get(mid, ""), ""
        return 1, "", "unexpected argv"

    def _all_envs(self):
        for envs in self.account_envelopes.values():
            for env in envs:
                yield env


def _env(mid, subject, sender="billing@shop.example", when="2026-10-02T09:00:00Z", attach=False):
    return {"id": mid, "subject": subject, "from": [{"email": sender}],
            "date": when, "has-attachment": attach}


def _sig(**kwargs):
    base = dict(account="formal", source_ref="mail:formal:1",
                sender="billing@shop.example", subject="", preview="",
                date="2026-10-02T09:00:00Z", now=NOW)
    base.update(kwargs)
    return extract_signals(**base)


def _policy():
    policy = load_policy(None)
    policy["_preferences"] = {"explicit": [], "inferred": [], "snoozes": []}
    return policy


# -- extraction ---------------------------------------------------------------


def test_extract_payment_and_order_categories():
    due = _sig(subject="Your payment is due on 10 Oct 2026")
    assert due and due[0]["category"] == "payment_due"
    assert due[0]["due_date"] == "2026-10-10" and due[0]["confidence"] == "uncertain"

    order = _sig(subject="Action required: confirm your order INV-48213 by 2026-10-10",
                 preview="Please confirm.")
    assert order and order[0]["category"] == "order_action"
    assert order[0]["ref_number"] == "inv48213"
    assert order[0]["due_date"] == "2026-10-10" and order[0]["confidence"] == "explicit"

    failed = _sig(subject="Payment failed for invoice XJ-9911")
    assert failed and failed[0]["category"] == "payment_failed"


def test_marketing_never_creates_obligation():
    assert _sig(subject="50% off sale ends tonight - shop now",
                preview="Your order awaits in the app, unsubscribe here") == []
    assert _sig(subject="Exclusive membership offer inside",
                preview="limited time deal") == []


def test_no_invented_due_date_is_uncertain():
    sig = _sig(subject="Your renewal is approaching")
    assert sig and sig[0]["category"] == "renewal"
    assert sig[0]["due_date"] == ""           # no explicit date -> no due date
    assert sig[0]["confidence"] == "uncertain"


def test_two_distinct_orders_same_sender():
    records = {}
    s1 = _sig(subject="Confirm your order INV-11111", source_ref="mail:formal:a")
    s2 = _sig(subject="Confirm your order INV-22222", source_ref="mail:formal:b")
    merge_signals(records, s1 + s2, NOW)
    assert len(records) == 2
    assert all(rec["status"] == "pending" for rec in records.values())


# -- lifecycle ----------------------------------------------------------------


def test_resolution_requires_matching_object_evidence():
    records = {}
    merge_signals(records, _sig(subject="Payment due for invoice INV-99999"), NOW)
    assert len(records) == 1
    # A resolution that does NOT share the object key (no ref, different text)
    # must not close the obligation.
    merge_signals(records, _sig(subject="Thank you for your payment",
                                sender="other@merchant.example",
                                source_ref="mail:formal:z"), NOW)
    assert all(rec["status"] == "pending" for rec in records.values())


def test_resolution_with_matching_ref_closes():
    records = {}
    merge_signals(records, _sig(subject="Payment due for invoice INV-77777"), NOW)
    merge_signals(records, _sig(subject="Payment received for invoice INV-77777",
                                source_ref="mail:formal:paid"), NOW)
    rec = next(iter(records.values()))
    assert rec["status"] == "satisfied"
    assert rec["resolved_by"] == "mail:formal:paid"


def test_disappearance_is_not_satisfaction():
    records = {}
    merge_signals(records, _sig(subject="Payment failed for invoice INV-55555"), NOW)
    # Later scan finds nothing (message aged out) -> no signals -> still pending.
    merge_signals(records, [], NOW + timedelta(days=2))
    assert all(rec["status"] == "pending" for rec in records.values())


def test_prune_closed_keeps_open_and_retention():
    records = {}
    merge_signals(records, _sig(subject="Payment due for invoice INV-33333"), NOW)
    merge_signals(records, _sig(subject="Payment received for invoice INV-33333"), NOW)
    open_rec = next(iter(records.values()))
    # add a second still-open record
    merge_signals(records, _sig(subject="Confirm your order INV-44444"), NOW)
    closed_key = next(k for k, v in records.items() if v["status"] == "satisfied")
    records[closed_key]["resolved_at"] = (NOW - timedelta(days=40)).isoformat()
    prune(records, NOW, retention_days=30)
    assert closed_key not in records
    assert any(rec["status"] == "pending" for rec in records.values())
    assert open_rec["status"] == "satisfied"


def test_retention_across_fresh_processes():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        data = new_store()
        merge_signals(data["records"], _sig(subject="Payment due for invoice INV-88888"), NOW)
        data["last_scan_at"] = NOW.isoformat()
        save_obligations(store, data)
        # A brand-new StateStore (fresh process) must see the persisted record.
        fresh = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        reloaded = load_obligations(fresh)
        assert len(reloaded["records"]) == 1
        assert health(reloaded)["pending"] == 1


# -- undated no urgency -------------------------------------------------------


def _obligation_packet(status="pending", due=""):
    item = {"ref": "obligation:formal:obj:ref:inv1", "id": "obj:ref:inv1",
            "title": "Confirm order INV-1", "category": "order_action",
            "status": status, "due_date": due, "all_day": bool(due and len(due) == 10),
            "updated": NOW.isoformat(), "source": "formal"}
    res = SourceResult("obligation:formal", "ok", count=1, items=[item], checked_at=NOW.isoformat())
    return build_packet([res], clock=SeededClock(NOW.isoformat()), policy=_policy(), baseline={})


def test_undated_obligation_has_no_urgency():
    packet = _obligation_packet(due="")
    assert packet["candidates"][0]["phase"] == "undated"
    prop = EvaluatorResult("notify", kind="obligation", key="obligation:formal:obj:ref:inv1",
                           message="An order needs action.", urgency="urgent",
                           evidence=["obligation:formal:obj:ref:inv1"],
                           consequence="order missed", confidence="high",
                           deadline=(NOW + timedelta(minutes=30)).isoformat())
    dec = decide(prop, packet, clock=SeededClock(NOW.isoformat()), policy=_policy(),
                 prefs={"explicit": [], "inferred": []}, state={})
    assert not dec["send"]
    assert _time_phase("obligation", "", now=NOW) == "undated"


# -- end-to-end collection + no leakage ---------------------------------------


def test_collect_obligations_end_to_end_and_no_raw_body_stored():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        secret_body = ("Signed link https://pay.example/checkout?token=SECRET123456 "
                       "card 4111111111111111")
        runner = MailRunner(
            {
                "formal": [_env("m1", "Action required: confirm your order INV-48213 by 2026-10-10")],
                "informal": [_env("m2", "Team lunch photos from Friday")],
            },
            bodies={"m1": secret_body, "m2": "plain non-actionable note"},
        )
        results = collect_obligations(_policy(), SeededClock(NOW.isoformat()), store=store, runner=runner)
        names = {r.name for r in results}
        assert names == {"obligation:formal", "obligation:informal"}
        formal = next(r for r in results if r.name == "obligation:formal")
        assert formal.count == 1 and formal.items[0]["category"] == "order_action"
        packet = build_packet(results, clock=SeededClock(NOW.isoformat()), policy=_policy(), baseline={})
        assert any(c["kind"] == "obligation" for c in packet["candidates"])
        # Only likely-action messages were previewed (metadata-first).
        reads = [c for c in runner.calls if "read" in c]
        assert len(reads) == 1 and reads[0][-1] == "m1"
        # No raw body, signed URL or card number is persisted anywhere.
        raw = (Path(tmp) / "obligations.json").read_text(encoding="utf-8")
        assert "SECRET123456" not in raw and "pay.example" not in raw
        assert "4111111111111111" not in raw
        assert "token=" not in raw


def test_shared_daily_cap_across_kinds():
    policy = _policy()
    policy["daily_normal_cap"] = 3
    state = {"delivered": [
        {"at": NOW.isoformat(), "signature": "a"},
        {"at": NOW.isoformat(), "signature": "b"},
        {"at": NOW.isoformat(), "signature": "c"},
    ]}
    packet = _obligation_packet()
    prop = EvaluatorResult("notify", kind="obligation", key="obligation:formal:obj:ref:inv1",
                           message="An obligation needs attention.", urgency="normal",
                           evidence=["obligation:formal:obj:ref:inv1"])
    dec = decide(prop, packet, clock=SeededClock(NOW.isoformat()), policy=policy,
                 prefs={"explicit": [], "inferred": []}, state=state)
    assert not dec["send"] and dec["reason"] == "daily cap"


def test_pause_blocks_reads():
    import proactive_pilot.main as main

    with tempfile.TemporaryDirectory() as tmp:
        prefs_path = Path(tmp) / "preferences.json"
        prefs_path.write_text(json.dumps({
            "schema": 1, "version": 0, "explicit": [], "inferred": [], "snoozes": [],
            "paused": {"active": True, "until": "", "provenance": "user"},
        }), encoding="utf-8")
        args = main.build_parser().parse_args([
            "--run", "--preferences", str(prefs_path),
            "--state-dir", str(Path(tmp) / "state"),
            "--seeded-clock", NOW.isoformat(),
        ])
        called = {"n": 0}

        def boom(*a, **k):
            called["n"] += 1
            raise AssertionError("sources must not be read while paused")

        original = main.default_sources
        main.default_sources = boom
        try:
            rc = main.run_once(args)
        finally:
            main.default_sources = original
        assert rc == 0 and called["n"] == 0


def test_pause_blocks_all_commands_with_empty_stdout():
    import contextlib
    import io
    import proactive_pilot.main as main

    for flag in ("--run", "--check", "--collect-only", "--dry-run"):
        with tempfile.TemporaryDirectory() as tmp:
            prefs_path = Path(tmp) / "preferences.json"
            prefs_path.write_text(json.dumps({
                "schema": 1, "explicit": [], "inferred": [], "snoozes": [],
                "paused": {"active": True, "until": "", "provenance": "user"},
            }), encoding="utf-8")
            args = main.build_parser().parse_args([
                flag, "--preferences", str(prefs_path),
                "--state-dir", str(Path(tmp) / "state"),
                "--seeded-clock", NOW.isoformat(),
            ])
            called = {"n": 0}

            def boom(*a, **k):
                called["n"] += 1
                raise AssertionError("paused run must not read sources")

            original = main.default_sources
            main.default_sources = boom
            out, err = io.StringIO(), io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = main.run_once(args)
            finally:
                main.default_sources = original
            assert rc == 0 and called["n"] == 0, flag
            assert out.getvalue() == "", f"{flag}: paused stdout must be empty"
            assert "skip-paused" in err.getvalue()


def test_cold_bootstrap_no_backlog_spam():
    old_date = (NOW - timedelta(days=80)).isoformat().replace("+00:00", "Z")
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        runner = MailRunner({
            "formal": [_env("old1", "Payment due for invoice INV-12345 by 2026-07-01", when=old_date)],
            "informal": [],
        })
        r1 = collect_obligations(_policy(), SeededClock(NOW.isoformat()), store=store, runner=runner)
        p1 = build_packet(r1, clock=SeededClock(NOW.isoformat()), policy=_policy(), baseline={})
        rec = load_obligations(store)["records"]
        assert len(rec) == 1
        only = next(iter(rec.values()))
        # An old explicit due date is preserved, not dropped and not satisfied.
        assert only["due_date"] == "2026-07-01" and only["status"] == "pending"
        # Subsequent daily scans over the aged-out window must not change material.
        for day in (1, 2, 3):
            later = SeededClock((NOW + timedelta(days=day)).isoformat())
            r2 = collect_obligations(_policy(), later, store=store,
                                     runner=MailRunner({"formal": [], "informal": []}))
            p2 = build_packet(r2, clock=later, policy=_policy(), baseline={})
            assert packet_digest(p1) == packet_digest(p2), f"day {day} re-materialised backlog"


def test_meaningful_lifecycle_signatures():
    from proactive_pilot.candidates import _event_version, candidate_signature

    base = {"kind": "obligation", "ref": "obligation:formal:obj", "version": "v1",
            "phase": "due_today", "subject": "Confirm your order"}
    assert candidate_signature(base) == candidate_signature({**base, "subject": "reworded"})
    assert candidate_signature(base) != candidate_signature({**base, "version": "v2"})
    assert candidate_signature(base) != candidate_signature({**base, "phase": "overdue"})
    item_a = {"title": "Confirm order INV-1", "category": "order_action",
              "status": "pending", "due_date": "2026-10-10"}
    item_b = {**item_a, "due_date": "2026-10-11"}
    assert _event_version(item_a) != _event_version(item_b)


def test_signed_url_and_otp_redaction():
    from proactive_pilot.collectors import _bounded_preview

    preview = _bounded_preview(
        "Pay here https://shop.example/checkout?token=SECRET1234567890abcdef "
        "or code 483920", 200)
    assert "shop.example" not in preview and "SECRET" not in preview
    assert "483920" not in preview and "redacted" in preview


# -- fairness: account rotation, stable cursor, preview de-duplication ----------


class TrackingRunner:
    """Native-route fake with a per-account opaque token cursor.

    The ``next_page`` token is emitted once per account (first page) so a second
    run resumes with the token instead of re-reading the first ids.
    """

    def __init__(self, account_envelopes, bodies=None):
        self.account_envelopes = account_envelopes
        self.bodies = bodies or {}
        self.calls: list[list[str]] = []
        self.reads: list[str] = []       # dedicated body previews (message read)
        self.meta_reads: list[str] = []  # native metadata gets

    @staticmethod
    def _is_native_list(argv):
        return "gmail" in argv and "list" in argv

    @staticmethod
    def _is_native_get(argv):
        return "gmail" in argv and "get" in argv

    def run(self, argv, timeout):
        self.calls.append(list(argv))
        if self._is_native_list(argv):
            account = argv[argv.index("-a") + 1]
            token = argv[argv.index("--page-token") + 1] if "--page-token" in argv else ""
            envs = self.account_envelopes.get(account, [])
            if token:
                return 0, json.dumps({"ids": [], "next_page": None}), ""
            ids = [{"id": str(e["id"]), "threadId": str(e["id"])} for e in envs]
            return 0, json.dumps({"ids": ids, "next_page": None}), ""
        if self._is_native_get(argv):
            mid = argv[-1]
            self.meta_reads.append(mid)
            env = next((e for e in self._all_envs() if str(e["id"]) == mid), {})
            frm = env.get("from") or [{}]
            sender = (frm[0] or {}).get("email") or ""
            headers = [
                {"name": "From", "value": str(sender)},
                {"name": "Subject", "value": str(env.get("subject") or "")},
                {"name": "Date", "value": str(env.get("date") or "")},
            ]
            return 0, json.dumps({"id": mid, "headers": headers}), ""
        if "envelope" in argv:
            account = argv[argv.index("-a") + 1]
            page = int(argv[argv.index("--page") + 1])
            envs = self.account_envelopes.get(account, [])
            return 0, json.dumps({"envelopes": envs if page == 1 else []}), ""
        if "read" in argv:
            mid = argv[argv.index("read") + 1]
            self.reads.append(mid)
            return 0, self.bodies.get(mid, "body"), ""
        return 1, "", "unexpected"

    def _all_envs(self):
        for envs in self.account_envelopes.values():
            for env in envs:
                yield env


def test_obligation_account_rotation_attempts_each_account():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        runner = TrackingRunner({"formal": [], "informal": []})
        # Two runs must attempt BOTH accounts across the rotation, even when the
        # first account's scan is slow/partial. The production obligations path
        # uses the supported NATIVE route, not the unsupported envelope search.
        collect_obligations(_policy(), SeededClock(NOW.isoformat()), store=store, runner=runner)
        native = {c[c.index("-a") + 1] for c in runner.calls
                  if "gmail" in c and "list" in c}
        assert "formal" in native and "informal" in native
        assert not any("search" in c for c in runner.calls), "no unsupported search"
        # The persisted account cursor flips the start order next run.
        cur = load_obligations(store).get("account_cursor")
        assert cur == 1


def test_obligation_cursor_advances_stably_not_past_unread_pages():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy()
        policy["source_page_size"] = 1  # a single metadata get per run
        policy["obligation_metadata_items_per_run"] = 1
        runner = TrackingRunner({
            "formal": [_env("m1", "Action required: confirm your order INV-48213 by 2026-10-10"),
                       _env("m2", "Action required: confirm your order INV-22222 by 2026-10-10")],
            "informal": [_env("m3", "Team lunch photos from Friday")],
        })
        collect_obligations(policy, SeededClock(NOW.isoformat()), store=store, runner=runner)
        cursors = load_obligations(store)["cursors"]
        # Metadata budget hit with unvisited ids remaining in the fetched page:
        # the within-page offset advances by exactly the consumed ids and the
        # bootstrap is NOT done, so unread ids are never skipped.
        assert cursors["formal"]["offset"] == 1
        assert cursors["formal"]["bootstrap_done"] is False


def test_obligation_known_stable_id_not_repreviewed():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy()
        policy["source_page_size"] = 50
        policy["obligation_metadata_items_per_run"] = 5
        envs = {"formal": [_env("m1", "Action required: confirm your order INV-48213 by 2026-10-10")],
                "informal": []}
        runner = TrackingRunner(envs, bodies={"m1": "please confirm order INV-48213"})
        collect_obligations(policy, SeededClock(NOW.isoformat()), store=store, runner=runner)
        assert getattr(runner, "reads", []) == ["m1"]
        # Fresh bootstrap: the SAME stable id must not be previewed again. Reset
        # the cursor so the same first page is re-read, proving id-level skip.
        data = load_obligations(store)
        data["cursors"]["formal"] = {"token": "", "offset": 0, "bootstrap_done": False,
                                     "previewed_ids": data["cursors"]["formal"]["previewed_ids"]}
        save_obligations(store, data)
        runner2 = TrackingRunner(envs, bodies={"m1": "please confirm order INV-48213"})
        later = SeededClock((NOW + timedelta(days=1)).isoformat())
        collect_obligations(policy, later, store=store, runner=runner2)
        assert getattr(runner2, "reads", []) == [], "known stable id was re-previewed"


# -- stable window cutoff, native page/token cursor, cost bounds ---------------


def test_obligation_uses_supported_native_route_and_small_page():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy()
        policy["source_page_size"] = 50
        policy["obligation_page_size"] = 10
        policy["obligation_metadata_items_per_run"] = 3
        runner = TrackingRunner({
            "formal": [_env("m1", "Action required: confirm your order INV-48213 by 2026-10-10")],
            "informal": [],
        })
        collect_obligations(policy, SeededClock(NOW.isoformat()), store=store, runner=runner)
        native_lists = [c for c in runner.calls if "gmail" in c and "list" in c]
        assert native_lists, "obligation scan must use the native gmail messages list route"
        assert not any("search" in c for c in runner.calls), "unsupported search must not run"
        for call in native_lists:
            # A fixed public query with a bounded UTC date; native page-size bounded.
            assert "--query" in call and "--max-results" in call, call
            query = call[call.index("--query") + 1]
            assert query.startswith("in:inbox after:") and query.count("/") == 2, call
            assert 1 <= int(call[call.index("--max-results") + 1]) <= 10, call
        # Metadata gets are bounded and use --format metadata with the minimum
        # needed headers only (never a body).
        native_gets = [c for c in runner.calls if "gmail" in c and "get" in c]
        assert 1 <= len(native_gets) <= 3, "metadata gets must be a few per account/run"
        for call in native_gets:
            assert call[call.index("--format") + 1] == "metadata", call
            assert call.count("--header") == 3, call


class _TimeoutRunner:
    """Every native list/get call exceeds the wall-clock bound (costly metadata)."""

    def __init__(self):
        self.calls = 0

    def run(self, argv, timeout):
        if "gmail" in argv:
            self.calls += 1
            import subprocess
            raise subprocess.TimeoutExpired(list(argv), timeout)
        return 0, "", ""


def test_costly_metadata_timeout_marks_partial_and_persists_cursor():
    from proactive_pilot.collectors import Budget

    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy()
        policy["obligation_max_pages_per_run"] = 1
        results = collect_obligations(policy, SeededClock(NOW.isoformat()), store=store,
                                      runner=_TimeoutRunner(), budget=Budget(15))
        assert {r.name for r in results} == {"obligation:formal", "obligation:informal"}
        for res in results:
            # A costly/timeout scan must never look like a fresh healthy check.
            assert res.truncated is True
            assert res.status != "ok"
            assert getattr(res, "scanned", 0) == 0
            assert "auth" not in (res.failure or "").lower()
        cursors = load_obligations(store)["cursors"]
        # Partial bootstrap state IS persisted for both accounts, so the next run
        # resumes the same cursor instead of silently losing coverage.
        assert set(cursors) == {"formal", "informal"}
        for cur in cursors.values():
            assert cur["bootstrap_done"] is False
        # The bootstrap window cutoff is persisted so new arrivals cannot shift it.
        assert load_obligations(store)["window_cutoff"]
        h = health(load_obligations(store))
        assert h["accounts_with_cursor"] == 2
        assert "metadata_scanned_total" in h


def test_fair_account_allocation_clamps_first_account():
    from proactive_pilot.collectors import Budget

    class _RecordingRunner:
        def __init__(self):
            self.timeouts: list[tuple[str, int]] = []

        def run(self, argv, timeout):
            if "gmail" in argv and "list" in argv:
                self.timeouts.append((argv[argv.index("-a") + 1], timeout))
                return 0, json.dumps({"ids": [], "next_page": None}), ""
            return 0, "", ""

    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy()
        policy["obligation_max_pages_per_run"] = 1
        runner = _RecordingRunner()
        collect_obligations(policy, SeededClock(NOW.isoformat()), store=store,
                            runner=runner, budget=Budget(15))
        assert len(runner.timeouts) == 2, "both accounts must be attempted"
        first_account, first_timeout = runner.timeouts[0]
        second_account, second_timeout = runner.timeouts[1]
        assert first_account != second_account
        # The first account is clamped to ~half the reserved slot; the second
        # still gets a positive window (it is not starved to zero).
        assert 1 <= first_timeout <= 8, (first_account, first_timeout)
        assert second_timeout >= 1, (second_account, second_timeout)


class _PagedRunner:
    """Native-route fake: returns distinct id pages via the opaque page token."""

    def __init__(self, pages):
        self.pages = pages
        self.calls: list[list[str]] = []

    def run(self, argv, timeout):
        self.calls.append(list(argv))
        if "gmail" in argv and "list" in argv:
            token = argv[argv.index("--page-token") + 1] if "--page-token" in argv else ""
            page = 1 if not token else int(token)
            envs = self.pages.get(page, [])
            nxt = page + 1 if (page + 1) in self.pages and self.pages.get(page + 1) else None
            ids = [{"id": str(e["id"]), "threadId": str(e["id"])} for e in envs]
            return 0, json.dumps({"ids": ids, "next_page": nxt}), ""
        if "gmail" in argv and "get" in argv:
            mid = argv[-1]
            env = next((e for envs in self.pages.values() for e in envs
                        if str(e["id"]) == mid), {})
            frm = env.get("from") or [{}]
            headers = [
                {"name": "From", "value": str((frm[0] or {}).get("email") or "")},
                {"name": "Subject", "value": str(env.get("subject") or "")},
                {"name": "Date", "value": str(env.get("date") or "")},
            ]
            return 0, json.dumps({"id": mid, "headers": headers}), ""
        return 0, "", ""


def test_no_premature_full_90day_completion_and_scanned_count():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy()
        policy["source_page_size"] = 2
        policy["obligation_page_size"] = 2
        policy["obligation_metadata_items_per_run"] = 2
        policy["obligation_max_pages_per_run"] = 1
        pages = {
            1: [_env("m1", "Action required: confirm your order INV-11111 by 2026-10-10"),
                _env("m2", "Action required: confirm your order INV-22222 by 2026-10-10")],
            2: [_env("m3", "Payment due for invoice INV-33333 by 2026-10-10")],
            3: [],
        }
        runner = _PagedRunner(pages)
        # First pass reads exactly one full native id page (token continues):
        # NOT the whole 90 days.
        r1 = collect_obligations(policy, SeededClock(NOW.isoformat()), store=store,
                                 runner=runner, budget=None)
        formal1 = next(r for r in r1 if r.name == "obligation:formal")
        assert formal1.window_complete is False and formal1.truncated is True
        assert formal1.scanned == 2, "safe metadata scanned count must be exposed"
        cur1 = load_obligations(store)["cursors"]["formal"]
        assert cur1["token"] == "2" and cur1["bootstrap_done"] is False
        # Only after the window is actually exhausted is bootstrap marked done.
        later = SeededClock((NOW + timedelta(days=1)).isoformat())
        collect_obligations(policy, later, store=store, runner=_PagedRunner(pages))
        cur2 = load_obligations(store)["cursors"]["formal"]
        assert cur2["bootstrap_done"] is True


# -- parent fix regression: normalized ISO-string start sorting -----------------


def test_calendar_sort_accepts_normalized_iso_string_starts():
    from proactive_pilot.collectors import _event_start_key

    # Raw Google-shaped events (start is a dict).
    raw_late = {"id": "a", "start": {"dateTime": "2026-10-03T18:00:00+08:00"}}
    raw_early = {"id": "b", "start": {"dateTime": "2026-10-03T09:00:00+08:00"}}
    # Normalized items (start is an ISO string) as produced by _calendar_item.
    norm_late = {"id": "c", "start": "2026-10-03T20:00:00+08:00"}
    norm_early = {"id": "d", "start": "2026-10-03T08:00:00+08:00"}
    ordered = sorted([raw_late, norm_late, raw_early, norm_early], key=_event_start_key)
    assert [e["id"] for e in ordered] == ["d", "b", "a", "c"]
    # A real-shaped nonempty normalized result list sorts without raising and
    # preserves ordering; an unparseable/missing start sorts last.
    mixed = [
        {"id": "x", "start": "2026-10-03T10:00:00+08:00"},
        {"id": "y", "start": ""},
        {"id": "z", "start": "2026-10-03T09:00:00+08:00"},
    ]
    mixed.sort(key=_event_start_key)
    assert [e["id"] for e in mixed] == ["z", "x", "y"]


# -- provider-native Gmail route: actual shapes, resume, fail-closed ------------


class _NativeRunner:
    """Emits the REAL native Gmail shapes (dict ids + top-level headers)."""

    def __init__(self, pages, next_by_page=None, page_by_token=None):
        self.pages = pages
        self.next_by_page = next_by_page or {}
        self.page_by_token = page_by_token or {}
        self.calls: list[list[str]] = []

    def run(self, argv, timeout):
        self.calls.append(list(argv))
        if "gmail" in argv and "list" in argv:
            token = argv[argv.index("--page-token") + 1] if "--page-token" in argv else ""
            if not token:
                page = 1
            elif token in self.page_by_token:
                page = self.page_by_token[token]
            else:
                page = int(token)
            envs = self.pages.get(page, [])
            ids = [{"id": str(e["id"]), "threadId": "th-" + str(e["id"])} for e in envs]
            nxt = self.next_by_page.get(page)
            return 0, json.dumps({"ids": ids, "next_page": nxt}), ""
        if "gmail" in argv and "get" in argv:
            mid = argv[-1]
            env = next((e for envs in self.pages.values() for e in envs
                        if str(e["id"]) == mid), {})
            # REAL top-level shape: no payload wrapper; headers list[{name,value}];
            # snippet is present but MUST be discarded, never retained.
            payload = {
                "id": mid, "thread-id": "th-" + mid, "label-ids": ["INBOX"],
                "snippet": "PRIVATE_SNIPPET_BODY_TOKEN", "size-estimate": 99,
                "internal-date": str(env.get("internal-date") or "2026-10-02T09:00:00Z"),
                "headers": [
                    {"name": "From", "value": str((env.get("from") or [{}])[0].get("email") or "")},
                    {"name": "Subject", "value": str(env.get("subject") or "")},
                    {"name": "Date", "value": str(env.get("date") or "2026-10-02T09:00:00Z")},
                ],
            }
            return 0, json.dumps(payload), ""
        return 0, "", ""


def _native_env(mid, subject, sender="billing@shop.example", when="2026-10-02T09:00:00Z"):
    return {"id": mid, "subject": subject, "from": [{"email": sender}],
            "date": when, "has-attachment": False}


def test_native_route_parses_dict_ids_and_toplevel_headers():
    from proactive_pilot.collectors import collect_himalaya_native

    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy()
        policy["obligation_metadata_items_per_run"] = 3
        runner = _NativeRunner({1: [
            _native_env("m1", "Action required: confirm your order INV-48213 by 2026-10-10"),
            _native_env("m2", "Payment due for invoice INV-77777 by 2026-10-10"),
        ]}, next_by_page={1: "opaque-token-XYZ"})
        res = collect_himalaya_native("formal", 10, clock=SeededClock(NOW.isoformat()),
                                      page_size=10, runner=runner, max_metadata=3)
        assert res.status == "ok" and res.count == 2
        # The snippet (which can carry body text) is discarded entirely.
        assert all("PRIVATE_SNIPPET" not in json.dumps(it) for it in res.items)
        assert res.items[0]["sender"] == "billing@shop.example"
        assert res.items[0]["ref"] == "mail:formal:m1"
        # Opaque token is persisted verbatim (never parsed/normalized).
        assert res.next_token == "opaque-token-XYZ"
        assert res.window_complete is False
        # Fixed argv: native list then metadata gets with exactly the 3 headers.
        assert runner.calls[0][:6] == ["himalaya", "-a", "formal", "gmail", "messages", "list"]
        gets = [c for c in runner.calls if "get" in c]
        assert gets and all(c[c.index("--format") + 1] == "metadata" for c in gets)
        assert all(c.count("--header") == 3 for c in gets)


def test_native_route_token_resume_and_within_page_offset():
    from proactive_pilot.collectors import collect_himalaya_native

    # (a) token resume: page 1 -> token T; page 2 keyed by that token.
    runner = _NativeRunner({1: [_native_env("a1", "Payment due for invoice INV-1 by 2026-10-10")],
                            2: [_native_env("b1", "Payment due for invoice INV-2 by 2026-10-10")]},
                           next_by_page={1: "T2"}, page_by_token={"T2": 2})
    r1 = collect_himalaya_native("formal", 10, clock=SeededClock(NOW.isoformat()),
                                 runner=runner, max_metadata=5)
    assert r1.next_token == "T2" and r1.offset == 0
    r2 = collect_himalaya_native("formal", 10, clock=SeededClock(NOW.isoformat()),
                                 runner=runner, max_metadata=5, start_token="T2")
    assert r2.count == 1 and r2.items[0]["id"] == "b1"
    assert "--page-token" in runner.calls[-2] and "T2" in runner.calls[-2]

    # (b) within-page offset: 3 ids, only 2 metadata gets -> offset=2, same page.
    runner2 = _NativeRunner({1: [_native_env("o1", "Payment due for invoice INV-1 by 2026-10-10"),
                                 _native_env("o2", "Payment due for invoice INV-2 by 2026-10-10"),
                                 _native_env("o3", "Payment due for invoice INV-3 by 2026-10-10")]})
    r3 = collect_himalaya_native("formal", 10, clock=SeededClock(NOW.isoformat()),
                                 runner=runner2, max_metadata=2)
    assert r3.offset == 2 and r3.window_complete is False and r3.next_token == ""
    # Resume at the offset reads exactly the unvisited third id, then completes.
    r4 = collect_himalaya_native("formal", 10, clock=SeededClock(NOW.isoformat()),
                                 runner=runner2, max_metadata=2, start_offset=r3.offset)
    assert r4.count == 1 and r4.items[0]["id"] == "o3" and r4.window_complete is True


def test_native_route_malformed_fails_closed_without_cursor_loss():
    from proactive_pilot.collectors import collect_himalaya_native

    class _Malformed:
        def run(self, argv, timeout):
            return 0, json.dumps({"unexpected": []}), ""

    res = collect_himalaya_native("formal", 10, clock=SeededClock(NOW.isoformat()),
                                  runner=_Malformed(), start_token="KEEP", start_offset=4)
    assert res.status == "error" and "parse" in res.error
    assert res.next_token == "KEEP" and res.offset == 4  # no cursor advance


def test_native_route_timeout_preserves_cursor():
    import subprocess
    from proactive_pilot.collectors import collect_himalaya_native

    class _Slow:
        def run(self, argv, timeout):
            raise subprocess.TimeoutExpired(list(argv), timeout)

    res = collect_himalaya_native("formal", 10, clock=SeededClock(NOW.isoformat()),
                                  runner=_Slow(), start_token="T", start_offset=1)
    assert res.status == "error" and res.next_token == "T" and res.offset == 1


def test_native_route_auth_error_fails_closed_single_call():
    from proactive_pilot.collectors import collect_himalaya_native

    class _Auth:
        def __init__(self):
            self.calls = 0

        def run(self, argv, timeout):
            self.calls += 1
            return 1, "", "error: 403 permission denied"

    runner = _Auth()
    res = collect_himalaya_native("formal", 10, clock=SeededClock(NOW.isoformat()), runner=runner)
    assert res.status == "error" and res.error == "authentication unavailable"
    # Auth failures are NEVER retried with a second (potentially expensive) call.
    assert runner.calls == 1


def test_native_route_unsupported_stdout_json_degrades_to_list():
    from proactive_pilot.collectors import collect_himalaya_native

    class _Unsupported:
        def __init__(self):
            self.cmds: list[str] = []

        def run(self, argv, timeout):
            self.cmds.append(argv[3] if len(argv) > 3 else "")
            if "gmail" in argv and "list" in argv:
                # EXACT live Gmail shape: exit 1, JSON error on STDOUT, empty stderr.
                return 1, json.dumps({"error": "Gmail does not support the shared "
                                               "envelope search", "sources": [],
                                      "backtrace": "..."}), ""
            if "envelope" in argv and "list" in argv:
                return 0, json.dumps({"envelopes": [
                    {"id": "e1", "subject": "Hi", "from": [{"email": "a@b"}],
                     "date": "2026-10-02T00:00:00Z"}]}), ""
            return 1, "", "unexpected"

    runner = _Unsupported()
    res = collect_himalaya_native("formal", 10, clock=SeededClock(NOW.isoformat()), runner=runner)
    assert res.status == "ok" and res.count == 1
    assert "envelope" in runner.cmds, "unsupported native route must degrade to plain list"


def test_collect_himalaya_detects_search_unsupported_on_stdout_not_auth():
    from proactive_pilot.collectors import collect_himalaya

    class _StdoutUnsupported:
        def __init__(self):
            self.calls: list[list[str]] = []

        def run(self, argv, timeout):
            self.calls.append(list(argv))
            if "search" in argv:
                return 1, json.dumps({"error": "Gmail does not support the shared "
                                               "envelope search", "sources": [],
                                      "backtrace": "..."}), ""
            return 0, json.dumps({"envelopes": [
                {"id": "e1", "subject": "Hi", "from": [{"email": "a@b"}],
                 "date": "2026-10-02T00:00:00Z"}]}), ""

    runner = _StdoutUnsupported()
    res = collect_himalaya("formal", 10, clock=SeededClock(NOW.isoformat()),
                           window_hours=48, runner=runner, server_window=True)
    assert res.status == "ok" and res.count == 1
    assert any("search" in c for c in runner.calls)
    assert any("list" in c for c in runner.calls), "stdout JSON error must degrade to list"


def test_bootstrap_window_cutoff_stable_across_new_arrivals():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy()
        policy["obligation_metadata_items_per_run"] = 1
        runner = _NativeRunner({1: [_native_env("a1", "Payment due for invoice INV-1 by 2026-10-10"),
                                    _native_env("a2", "Payment due for invoice INV-2 by 2026-10-10")]})
        collect_obligations(policy, SeededClock(NOW.isoformat()), store=store, runner=runner)
        cutoff1 = load_obligations(store)["window_cutoff"]
        assert cutoff1
        # A later run (new arrivals possible) must reuse the SAME cutoff so the
        # cursor cannot shift and skip unvisited stable ids.
        later = SeededClock((NOW + timedelta(days=3)).isoformat())
        collect_obligations(policy, later, store=store,
                            runner=_NativeRunner({1: []}))
        assert load_obligations(store)["window_cutoff"] == cutoff1


def test_native_default_silence_and_global_pause():
    import contextlib
    import io
    import json as _json
    import proactive_pilot.main as main
    import proactive_pilot.obligations as obligations
    import proactive_pilot.publicfeeds as publicfeeds

    # (a) default run with the REAL native obligations wiring stays silent on
    # stdout even when the native route reports a malformed/unavailable scan.
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp) / "state"
        prefs = Path(tmp) / "prefs.json"
        prefs.write_text(_json.dumps({"schema": 1, "explicit": [], "inferred": [],
                                      "snoozes": [], "paused": {"active": False}}))
        args = main.build_parser().parse_args([
            "--run", "--preferences", str(prefs), "--state-dir", str(state_dir),
            "--seeded-clock", NOW.isoformat(), "--no-model",
        ])

        def fake_default_sources(policy, clock, budget=None):
            return [lambda: SourceResult("tasks:primary", "ok", count=0, items=[],
                                         checked_at=clock.now().isoformat())]

        orig_ds = main.default_sources
        orig_feeds = publicfeeds.collect_public_feeds
        orig_obl = obligations.collect_obligations
        main.default_sources = fake_default_sources
        publicfeeds.collect_public_feeds = lambda *a, **k: []
        # NEVER touch a real account from a test: the obligation pipeline is
        # stubbed to a safe empty native scan.
        obligations.collect_obligations = lambda *a, **k: []
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = main.run_once(args)
        finally:
            main.default_sources = orig_ds
            publicfeeds.collect_public_feeds = orig_feeds
            obligations.collect_obligations = orig_obl
        assert rc == 0 and out.getvalue() == ""

    # (b) global pause: no source read at all, stdout empty.
    with tempfile.TemporaryDirectory() as tmp:
        prefs = Path(tmp) / "prefs.json"
        prefs.write_text(_json.dumps({"schema": 1, "explicit": [], "inferred": [],
                                      "snoozes": [],
                                      "paused": {"active": True, "until": "", "provenance": "user"}}))
        args = main.build_parser().parse_args([
            "--collect-only", "--preferences", str(prefs),
            "--state-dir", str(Path(tmp) / "state"), "--seeded-clock", NOW.isoformat(),
        ])
        called = {"n": 0}

        def boom(*a, **k):
            called["n"] += 1
            raise AssertionError("paused run must not read sources")

        orig = main.default_sources
        main.default_sources = boom
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main.run_once(args)
        finally:
            main.default_sources = orig
        assert rc == 0 and called["n"] == 0 and out.getvalue() == ""
        assert "skip-paused" in err.getvalue()


import unittest as _unittest


class AwarenessControls(_unittest.TestCase):
    pass


def _attach_tests(cls):
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            setattr(cls, _name, (lambda self, _fn=_fn: _fn()))


_attach_tests(AwarenessControls)


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {test.__name__}: {exc!r}")
    print(f"\n{passed}/{len(tests)} obligation tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
