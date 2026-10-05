"""Parent controls for the provider-native Gmail metadata cursor.

These are the explicit negative/regression controls the parent asked for. Every
runner here is an EXPLICIT synthetic fake; the default ``SubprocessRunner`` is
never used, so no live/personal account is ever read. Fixtures only.

Covers:
  * multi-page partial token: 10 + 10 ids with 3 metadata gets/run must observe
    ALL 20 exactly, with no skip and no duplicate;
  * insertion stability: 3 brand-new ids inserted at the head of page 1 between
    runs must not displace/omit any original id;
  * native list argv carries the provider token ONLY once the saved page queue is
    fully visited (no mid-page re-list);
  * malformed payload / non-auth get error never declares the window complete
    and never skips the current unvisited id (it is retried);
  * exact response-id mismatch and ill-shaped headers fail closed;
  * auth failure on a metadata get is a safe error status (not healthy partial);
  * ``scanned`` counts actual successful gets, ``ids_listed`` counts listed ids.

Standalone (no pytest needed):
  PYTHONPATH=src python3 tests/test_parent_native_cursor.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import collect_himalaya_native  # noqa: E402
from proactive_pilot.obligations import collect_obligations, load_obligations  # noqa: E402
from proactive_pilot.policy import load_policy  # noqa: E402
from proactive_pilot.state import StateStore  # noqa: E402

SG = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=SG)


def _env(mid, subject="Action required: confirm your order INV-0001 by 2026-10-10",
         sender="billing@shop.example", when="2026-10-02T09:00:00Z"):
    return {"id": mid, "subject": subject, "from": [{"email": sender}],
            "date": when, "has-attachment": False}


def _policy():
    policy = load_policy(None)
    policy["_preferences"] = {"explicit": [], "inferred": [], "snoozes": []}
    return policy


class FakeNativeRunner:
    """Explicit synthetic native Gmail route. NEVER a real subprocess."""

    def __init__(self, pages, *, next_by_page=None, page_for_token=None,
                 fail_get=None, malformed_get=None, mismatch_id=None,
                 bad_headers=None, timeout=False):
        self.pages = pages                              # page -> [env, ...]
        self.next_by_page = next_by_page or {}          # page -> opaque token
        self.page_for_token = page_for_token or {}      # token -> page
        self.fail_get = fail_get or {}                  # id -> (code, out, err)
        self.malformed_get = set(malformed_get or ())   # id -> non-JSON
        self.mismatch_id = set(mismatch_id or ())       # id -> wrong response id
        self.bad_headers = set(bad_headers or ())       # id -> headers not a list
        self.timeout = timeout
        self.calls: list[list[str]] = []
        self.get_ids: list[str] = []
        self.list_tokens: list[str] = []

    def _all_envs(self):
        for envs in self.pages.values():
            for env in envs:
                yield env

    def run(self, argv, timeout):
        import subprocess

        self.calls.append(list(argv))
        if "gmail" in argv and "list" in argv:
            if self.timeout:
                raise subprocess.TimeoutExpired(list(argv), timeout)
            token = argv[argv.index("--page-token") + 1] if "--page-token" in argv else ""
            self.list_tokens.append(token)
            page = self.page_for_token.get(token, 1) if token else 1
            envs = self.pages.get(page, [])
            ids = [{"id": str(e["id"]), "threadId": "th-" + str(e["id"])} for e in envs]
            return 0, json.dumps({"ids": ids, "next_page": self.next_by_page.get(page)}), ""
        if "gmail" in argv and "get" in argv:
            mid = argv[-1]
            self.get_ids.append(mid)
            if mid in self.fail_get:
                return self.fail_get[mid]
            if mid in self.malformed_get:
                return 0, "{not-json", ""
            env = next((e for e in self._all_envs() if str(e["id"]) == mid), {})
            headers = [
                {"name": "From", "value": str((env.get("from") or [{}])[0].get("email") or "")},
                {"name": "Subject", "value": str(env.get("subject") or "")},
                {"name": "Date", "value": str(env.get("date") or "")},
            ]
            if mid in self.bad_headers:
                headers = {"not": "a-list"}
            resp_id = "OTHER-ID" if mid in self.mismatch_id else mid
            return 0, json.dumps({"id": resp_id, "headers": headers,
                                  "snippet": "PRIVATE"}), ""
        return 1, "", "unexpected argv"


def _drive(pages, *, next_by_page=None, page_for_token=None, max_metadata=3,
           page_size=10, limit=100, runs=30, runner=None):
    """Drive collect_himalaya_native across runs with a persisted queue cursor."""
    runner = runner or FakeNativeRunner(
        pages, next_by_page=next_by_page, page_for_token=page_for_token)
    state = {"pending_ids": [], "token": "", "offset": 0}
    seen: list[str] = []
    for _ in range(runs):
        res = collect_himalaya_native(
            "formal", limit, clock=SeededClock(NOW.isoformat()), page_size=page_size,
            runner=runner, max_metadata=max_metadata,
            start_token=state["token"], start_offset=state["offset"],
            pending_ids=state["pending_ids"],
        )
        seen.extend(str(it["id"]) for it in res.items)
        if res.window_complete:
            state = {"pending_ids": [], "token": "", "offset": 0}
            break
        state = {"pending_ids": list(res.pending_ids), "token": str(res.next_token),
                 "offset": int(res.offset)}
    return seen, runner, state


# -- multi-page / partial token negative control -------------------------------


def test_multipage_partial_token_observes_all_ids_exactly():
    p1 = [f"p{i:02d}" for i in range(1, 11)]
    p2 = [f"p{i:02d}" for i in range(11, 21)]
    pages = {1: [_env(m) for m in p1], 2: [_env(m) for m in p2]}
    runner = FakeNativeRunner(pages, next_by_page={1: "P2"}, page_for_token={"P2": 2})
    seen, runner, _ = _drive(None, runner=runner)
    # EXACTLY all 20, no skip and no duplicate.
    assert seen == p1 + p2, seen
    assert len(set(seen)) == 20
    # The provider token is only used once the page-1 queue is fully visited.
    assert runner.list_tokens == ["", "P2"], runner.list_tokens
    # No list call happened while the page-1 queue still had unvisited ids:
    # 10 ids at 3/run is 4 runs, so the 5th run is the first re-list.
    list_runs = [i for i, c in enumerate(runner.calls) if "gmail" in c and "list" in c]
    assert len(list_runs) == 2


def test_three_new_arrivals_at_head_do_not_omit_originals():
    p1 = [f"p{i:02d}" for i in range(1, 11)]
    p2 = [f"p{i:02d}" for i in range(11, 21)]
    pages = {1: [_env(m) for m in p1], 2: [_env(m) for m in p2]}
    runner = FakeNativeRunner(pages, next_by_page={1: "P2"}, page_for_token={"P2": 2})
    # First run only (lists page 1, consumes 3 ids).
    r1 = collect_himalaya_native("formal", 100, clock=SeededClock(NOW.isoformat()),
                                 page_size=10, runner=runner, max_metadata=3,
                                 pending_ids=[])
    assert r1.pending_ids == p1 and r1.offset == 3
    # Three brand-new messages arrive at the HEAD of page 1 before the next run.
    pages[1] = [_env("new1"), _env("new2"), _env("new3")] + [_env(m) for m in p1]
    state = {"pending_ids": list(r1.pending_ids), "token": str(r1.next_token),
             "offset": int(r1.offset)}
    seen = [str(it["id"]) for it in r1.items]
    for _ in range(30):
        res = collect_himalaya_native(
            "formal", 100, clock=SeededClock(NOW.isoformat()), page_size=10,
            runner=runner, max_metadata=3, start_token=state["token"],
            start_offset=state["offset"], pending_ids=state["pending_ids"])
        seen.extend(str(it["id"]) for it in res.items)
        if res.window_complete:
            break
        state = {"pending_ids": list(res.pending_ids), "token": str(res.next_token),
                 "offset": int(res.offset)}
    assert seen == p1 + p2, seen
    assert set(seen) >= set(p1) | set(p2)


# -- fail-closed get handling --------------------------------------------------


def test_malformed_get_never_complete_and_id_is_retried_not_skipped():
    pages = {1: [_env("m1"), _env("m2"), _env("m3")]}
    bad = FakeNativeRunner(pages, malformed_get={"m2"})
    r1 = collect_himalaya_native("formal", 100, clock=SeededClock(NOW.isoformat()),
                                 page_size=10, runner=bad, max_metadata=3,
                                 pending_ids=[])
    assert r1.window_complete is False
    assert r1.offset == 1 and r1.pending_ids == ["m1", "m2", "m3"]
    assert [it["id"] for it in r1.items] == ["m1"]
    assert r1.scanned == 1 and r1.ids_listed == 3
    assert "parse" in (r1.failure or "").lower()
    # The unvisited id is retried (never skipped) and the window completes only
    # after it is genuinely assessed.
    good = FakeNativeRunner(pages)
    r2 = collect_himalaya_native("formal", 100, clock=SeededClock(NOW.isoformat()),
                                 page_size=10, runner=good, max_metadata=3,
                                 start_token=r1.next_token, start_offset=r1.offset,
                                 pending_ids=r1.pending_ids)
    assert [it["id"] for it in r2.items] == ["m2", "m3"]
    assert r2.window_complete is True
    assert good.get_ids == ["m2", "m3"]


def test_nonauth_get_error_never_complete_and_preserves_unvisited():
    pages = {1: [_env("m1"), _env("m2"), _env("m3")]}
    runner = FakeNativeRunner(pages, fail_get={"m2": (1, "", "temporary backend error")})
    res = collect_himalaya_native("formal", 100, clock=SeededClock(NOW.isoformat()),
                                  page_size=10, runner=runner, max_metadata=3,
                                  pending_ids=[])
    assert res.window_complete is False
    assert res.offset == 1 and res.pending_ids == ["m1", "m2", "m3"]
    assert [it["id"] for it in res.items] == ["m1"]
    assert res.scanned == 1 and res.ids_listed == 3
    assert res.status == "ok" and res.truncated is True


def test_exact_id_mismatch_fails_closed_without_consuming_id():
    pages = {1: [_env("m1"), _env("m2")]}
    runner = FakeNativeRunner(pages, mismatch_id={"m2"})
    res = collect_himalaya_native("formal", 100, clock=SeededClock(NOW.isoformat()),
                                  page_size=10, runner=runner, max_metadata=3,
                                  pending_ids=[])
    assert res.window_complete is False
    assert res.offset == 1 and res.pending_ids == ["m1", "m2"]
    assert [it["id"] for it in res.items] == ["m1"]
    assert "mismatch" in (res.failure or "").lower()


def test_ill_shaped_headers_fail_closed():
    pages = {1: [_env("m1"), _env("m2")]}
    runner = FakeNativeRunner(pages, bad_headers={"m2"})
    res = collect_himalaya_native("formal", 100, clock=SeededClock(NOW.isoformat()),
                                  page_size=10, runner=runner, max_metadata=3,
                                  pending_ids=[])
    assert res.window_complete is False
    assert res.offset == 1 and res.pending_ids == ["m1", "m2"]
    assert [it["id"] for it in res.items] == ["m1"]


def test_auth_get_failure_is_safe_error_status_not_healthy_partial():
    pages = {1: [_env("m1"), _env("m2")]}
    runner = FakeNativeRunner(pages, fail_get={"m1": (1, "", "error: 403 permission denied")})
    res = collect_himalaya_native("formal", 100, clock=SeededClock(NOW.isoformat()),
                                  page_size=10, runner=runner, max_metadata=3,
                                  pending_ids=[])
    assert res.status == "error"
    assert res.error == "authentication unavailable"
    assert res.window_complete is False
    assert res.offset == 0 and res.pending_ids == ["m1", "m2"]
    assert res.scanned == 0
    assert [it["id"] for it in res.items] == []


def test_scanned_counts_successful_gets_not_listed_ids():
    pages = {1: [_env(m) for m in ("a", "b", "c", "d", "e")]}
    runner = FakeNativeRunner(pages, fail_get={"c": (1, "", "temporary backend error")})
    res = collect_himalaya_native("formal", 100, clock=SeededClock(NOW.isoformat()),
                                  page_size=10, runner=runner, max_metadata=5,
                                  pending_ids=[])
    assert res.ids_listed == 5
    assert res.scanned == 2, "only successful, projectable gets may count as assessed"
    assert res.scanned != res.ids_listed


def test_list_timeout_preserves_saved_queue():
    pages = {1: [_env("m1"), _env("m2")]}
    runner = FakeNativeRunner(pages, timeout=True)
    # The saved queue is drained (offset == len), so a next-page LIST is due; it
    # times out and the drained queue + token/offset are preserved for retry.
    res = collect_himalaya_native("formal", 100, clock=SeededClock(NOW.isoformat()),
                                  page_size=10, runner=runner, max_metadata=3,
                                  pending_ids=["keep1", "keep2"], start_offset=2,
                                  start_token="TOK")
    assert res.status == "error" and res.error == "timeout"
    assert res.pending_ids == ["keep1", "keep2"]
    assert res.offset == 2 and res.next_token == "TOK"


# -- obligations-level: incremental cursor + unresolved survival --------------


def test_incremental_partial_does_not_reset_cursor_each_run():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy()
        policy["source_page_size"] = 50
        policy["obligation_page_size"] = 10
        policy["obligation_metadata_items_per_run"] = 1
        # Run 1: empty page completes the bootstrap immediately (no ids).
        collect_obligations(policy, SeededClock(NOW.isoformat()), store=store,
                            runner=FakeNativeRunner({1: []}))
        cur = load_obligations(store)["cursors"]["formal"]
        assert cur["bootstrap_done"] is True
        # Runs 2..3: the incremental window has 4 ids but only 1 get/run. The
        # persisted incremental queue must advance 1 -> 2, never reset to 0/1.
        inc_pages = {1: [_env(m) for m in ("i1", "i2", "i3", "i4")]}
        collect_obligations(policy, SeededClock(NOW.isoformat()), store=store,
                            runner=FakeNativeRunner(inc_pages))
        cur = load_obligations(store)["cursors"]["formal"]
        assert cur["inc_cutoff"] and cur["inc_done"] is False
        assert cur["inc_offset"] == 1, cur
        assert cur["inc_pending_ids"] == ["i1", "i2", "i3", "i4"], cur
        later = SeededClock((NOW + timedelta(hours=1)).isoformat())
        collect_obligations(policy, later, store=store,
                            runner=FakeNativeRunner(inc_pages))
        cur = load_obligations(store)["cursors"]["formal"]
        assert cur["inc_offset"] == 2, "incremental cursor must not reset mid-window"
        assert cur["inc_pending_ids"] == ["i1", "i2", "i3", "i4"], cur


def test_unresolved_obligation_survives_source_unavailable_as_stale():
    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp), SeededClock(NOW.isoformat()))
        policy = _policy()
        policy["source_page_size"] = 50
        policy["obligation_metadata_items_per_run"] = 3
        good = FakeNativeRunner({1: [
            _env("m1", "Payment due for invoice INV-77777 by 2026-10-10")]})
        collect_obligations(policy, SeededClock(NOW.isoformat()), store=store, runner=good)
        records = load_obligations(store)["records"]
        assert any(r.get("status") == "pending" for r in records.values())
        # A later run cannot reach the provider at all: unresolved obligations
        # survive in state and are reported under a stale status (no urgency).
        results = collect_obligations(policy, SeededClock(NOW.isoformat()), store=store,
                                      runner=FakeNativeRunner({1: []}, timeout=True))
        formal = next(r for r in results if r.name == "obligation:formal")
        assert formal.status == "stale"
        assert formal.count >= 1 and formal.items
        assert all(it.get("status") == "pending" for it in formal.items)
        # The record is retained (never closed by absence).
        assert any(r.get("status") == "pending"
                   for r in load_obligations(store)["records"].values())


import unittest as _unittest


class ParentNativeCursor(_unittest.TestCase):
    pass


def _attach(cls):
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            setattr(cls, _name, (lambda self, _fn=_fn: _fn()))


_attach(ParentNativeCursor)


def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
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
    print(f"\n{passed}/{len(tests)} parent native cursor tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
