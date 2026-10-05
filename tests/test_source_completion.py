"""Source-completion tests: fixed argv, pagination, partial coverage, read-only
previews that never mutate Seen, credential redaction, time phases, stable digest.

Runs with the Hermes bootstrapped Python (pytest not required):
  TMPDIR=<scratch> python tests/test_source_completion.py

All fixtures are labelled synthetic. No network and no production source is read
by these tests.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from proactive_pilot.candidates import (  # noqa: E402
    _event_version,
    _time_phase,
    build_packet,
    candidate_signature,
    packet_digest,
)
from proactive_pilot.clock import SeededClock  # noqa: E402
from proactive_pilot.collectors import (  # noqa: E402
    ALLOWED_MAIL_ACCOUNTS,
    SourceResult,
    _calendar_batch,
    _calendar_item,
    _task_item,
    _tasklists_paged,
    _tasks_paged,
    collect_himalaya,
    collect_last_dm_preferences,
    default_sources,
    _google_aliases,
)
from proactive_pilot.policy import DEFAULT_POLICY  # noqa: E402

SG = ZoneInfo("Asia/Singapore")


def _policy():
    return json.loads(json.dumps(DEFAULT_POLICY))


class MailRunner:
    """Recording fixed-argv runner. Envelope pages + message-read bodies only."""

    def __init__(self, pages, bodies=None):
        self.pages = pages
        self.bodies = bodies or {}
        self.calls: list[list[str]] = []
        self.seen: set[str] = set()

    def run(self, argv, timeout):
        self.calls.append(list(argv))
        if "envelope" in argv:
            page = int(argv[argv.index("--page") + 1])
            return 0, json.dumps({"envelopes": self.pages.get(page, [])}), ""
        if "read" in argv:
            message_id = argv[argv.index("read") + 1]
            if "--seen" in argv:
                self.seen.add(message_id)
            return 0, self.bodies.get(message_id, "plain body text"), ""
        return 1, "", "unexpected argv"


def _env(mid, when, subject="Hello", sender="person@example.com", attach=False):
    return {"id": mid, "subject": subject, "from": [{"email": sender}],
            "date": when, "has-attachment": attach}


class FakeClient:
    """Minimal stand-in for the secretary Calendar/Tasks clients (no network)."""

    base = "https://tasks.example/v1"
    calendar_base = "https://cal.example/v1/calendars"

    def __init__(self, pages):
        self.pages = pages
        self.tokens: list[str | None] = []

    def _events(self, calendar_id):
        return f"{self.calendar_base}/{calendar_id}/events"

    def _tasks(self, tasklist_id):
        return f"{self.base}/lists/{tasklist_id}/tasks"

    def _request(self, method, url):
        token = None
        if "pageToken=" in url:
            token = url.split("pageToken=")[1].split("&")[0]
        self.tokens.append(token)
        return self.pages.get(token, {"items": []})


# -- fixed argv ---------------------------------------------------------------


def test_himalaya_fixed_argv_and_no_seen():
    clock = SeededClock("2026-10-03T10:00:00")
    runner = MailRunner({1: [_env("a", "2026-10-03T09:00:00Z")]}, {"a": "Body text"})
    res = collect_himalaya("formal", 15, clock=clock, window_hours=48, page_size=50,
                           runner=runner, preview_limit=1, server_window=True)
    assert res.status == "ok" and res.count == 1
    # The date window is pushed server-side (verified shared query DSL) instead
    # of hydrating the whole mailbox and filtering locally.
    assert runner.calls[0] == [
        "himalaya", "-a", "formal", "envelope", "search", "--json",
        "--page", "1", "--page-size", "50",
        "after", "2026-10-01",
    ]
    reads = [call for call in runner.calls if "read" in call]
    assert len(reads) == 1
    assert reads[0] == ["himalaya", "-a", "formal", "message", "read", "a"]
    assert "--seen" not in reads[0]
    assert runner.seen == set()  # read left the seen state untouched


def test_search_backend_mismatch_degrades_to_list_argv():
    clock = SeededClock("2026-10-03T10:00:00")

    class FallbackRunner:
        def __init__(self):
            self.calls: list[list[str]] = []

        def run(self, argv, timeout):
            self.calls.append(list(argv))
            if "envelope" in argv and "search" in argv:
                return 1, "", "error: unrecognized subcommand 'search'"
            if "envelope" in argv:
                return 0, json.dumps({"envelopes": [_env("a", "2026-10-03T09:00:00Z")]}), ""
            return 0, "", ""

    runner = FallbackRunner()
    res = collect_himalaya("formal", 15, clock=clock, window_hours=48, runner=runner,
                           server_window=True)
    assert res.status == "ok" and res.count == 1
    assert any("search" in c for c in runner.calls), "server-window attempted first"
    assert any("list" in c for c in runner.calls), "unsupported search degrades to list"


def test_unauthorized_mail_account_rejected():
    clock = SeededClock("2026-10-03T10:00:00")
    res = collect_himalaya("work", 5, clock=clock, runner=MailRunner({}))
    assert res.status == "error" and "not authorized" in res.error
    assert set(ALLOWED_MAIL_ACCOUNTS) == {"formal", "informal"}


# -- pagination + window + partial coverage -----------------------------------


def test_mail_window_excludes_old_and_is_complete():
    clock = SeededClock("2026-10-03T10:00:00")  # cutoff ~2026-10-01T10:00
    runner = MailRunner({1: [_env("new", "2026-10-03T09:00:00Z"),
                             _env("old", "2026-09-20T09:00:00Z")]})
    res = collect_himalaya("formal", 15, clock=clock, window_hours=48, runner=runner)
    assert res.status == "ok" and res.count == 1
    assert res.truncated is False
    assert res.items[0]["ref"] == "mail:formal:new"


def test_mail_paginates_until_empty_page():
    clock = SeededClock("2026-10-03T10:00:00")
    runner = MailRunner({
        1: [_env("p1", "2026-10-03T09:00:00Z")],
        2: [_env("p2", "2026-10-03T08:00:00Z")],
        3: [],
    })
    res = collect_himalaya("informal", 15, clock=clock, window_hours=48, page_size=1, runner=runner)
    assert res.count == 2 and res.truncated is False
    assert [call[call.index("--page") + 1] for call in runner.calls] == ["1", "2", "3"]


def test_mail_cap_marks_partial_not_healthy():
    clock = SeededClock("2026-10-03T10:00:00")
    runner = MailRunner({1: [_env("m1", "2026-10-03T09:55:00Z"),
                             _env("m2", "2026-10-03T09:54:00Z")]})
    res = collect_himalaya("formal", 1, clock=clock, window_hours=48, runner=runner)
    assert res.count == 1 and res.truncated is True
    packet = build_packet([res], clock=clock, policy=_policy(), baseline={})
    assert packet["coverage"]["mail:formal"]["status"] == "partial"
    assert packet["coverage"]["mail:formal"]["truncated"] is True


def test_calendar_nextpagetoken_pagination():
    pages = {
        None: {"items": [{"id": "e1"}], "nextPageToken": "t2"},
        "t2": {"items": [{"id": "e2"}]},
    }
    client = FakeClient(pages)
    batch, more = _calendar_batch(client, "primary", "a", "b", remaining=10, page_size=50)
    assert [e["id"] for e in batch] == ["e1", "e2"] and more is False
    assert client.tokens == [None, "t2"]

    capped, more_capped = _calendar_batch(client, "primary", "a", "b", remaining=1, page_size=50)
    assert capped[0]["id"] == "e1" and more_capped is True


def test_tasks_tasklist_and_task_pagination():
    pages = {
        None: {"items": [{"id": "L1"}], "nextPageToken": "n2"},
        "n2": {"items": [{"id": "L2"}]},
    }
    lists, more = _tasklists_paged(FakeClient(pages))
    assert [lst["id"] for lst in lists] == ["L1", "L2"] and more is False

    task_pages = {
        None: {"items": [{"id": "t1"}], "nextPageToken": "z2"},
        "z2": {"items": [{"id": "t2"}]},
    }
    tasks, more_tasks = _tasks_paged(FakeClient(task_pages), "L1",
                                     due_max="2026-10-04T00:00:00Z", remaining=10, page_size=50)
    assert [t["id"] for t in tasks] == ["t1", "t2"] and more_tasks is False


# -- credential / OTP handling ------------------------------------------------


def test_sensitive_message_redacted_and_never_read():
    clock = SeededClock("2026-10-03T10:00:00")
    runner = MailRunner({1: [_env("otp1", "2026-10-03T09:00:00Z",
                                  subject="Your OTP verification code",
                                  sender="no-reply@bank.example")]})
    res = collect_himalaya("formal", 15, clock=clock, runner=runner, preview_limit=1)
    item = res.items[0]
    assert item["sensitive"] is True
    assert "otp" not in item["subject"].lower()
    assert item["preview"] == "[redacted: sensitive message]"
    assert not [call for call in runner.calls if "read" in call]


def test_preview_bounds_and_redacts_credentials():
    clock = SeededClock("2026-10-03T10:00:00")
    body = "Meeting notes. Your code is 483920 and token abcdefghijklmnopqrstuvwxyz"
    runner = MailRunner({1: [_env("n1", "2026-10-03T09:00:00Z", subject="Project notes")]},
                        {"n1": body})
    res = collect_himalaya("formal", 15, clock=clock, runner=runner, preview_limit=1, preview_chars=120)
    preview = res.items[0]["preview"]
    assert len(preview) <= 120
    assert "483920" not in preview and "abcdefghijklmnopqrstuvwxyz" not in preview
    assert "redacted" in preview


# -- stable refs / version / cancelled status ---------------------------------


def test_refs_include_account_list_calendar():
    event = _calendar_item("primary", "cal123", {"summary": "Work"},
                           {"id": "e1", "summary": "Standup", "start": {"dateTime": "2026-10-03T11:00:00+08:00"}})
    assert event["ref"] == "cal:primary:cal123:e1"
    task = _task_item("informal", {"id": "t1", "_list_id": "L9", "status": "needsAction", "title": "Essay"},
                      date(2026, 10, 4))
    assert task["ref"] == "task:informal:L9:t1"


def test_cancelled_status_is_kept_and_affects_version():
    base = {"id": "e1", "summary": "Dentist", "start": {"dateTime": "2026-10-03T11:00:00+08:00"}}
    confirmed = _calendar_item("primary", "cal", {}, {**base, "status": "confirmed"})
    cancelled = _calendar_item("primary", "cal", {}, {**base, "status": "cancelled"})
    assert confirmed["status"] == "confirmed" and cancelled["status"] == "cancelled"
    assert _event_version(confirmed) != _event_version(cancelled)


# -- time phases --------------------------------------------------------------


def test_event_and_task_time_phases():
    clock = SeededClock("2026-10-03T10:00:00")
    now = clock.now()
    assert _time_phase("calendar", (now + timedelta(minutes=10)).isoformat(), now=now) == "within30min"
    assert _time_phase("calendar", (now + timedelta(minutes=60)).isoformat(), now=now) == "within90min"
    assert _time_phase("calendar", (now + timedelta(hours=5)).isoformat(), now=now) == "far"
    assert _time_phase("calendar", (now - timedelta(minutes=5)).isoformat(), now=now) == "passed"
    assert _time_phase("calendar", "2026-10-03", now=now, all_day=True) == "allday"
    assert _time_phase("tasks", "2026-10-02", now=now) == "overdue"
    assert _time_phase("tasks", "2026-10-03", now=now) == "due_today"
    assert _time_phase("tasks", "2026-10-04", now=now) == "approaching"
    assert _time_phase("tasks", "2026-10-20", now=now) == "far"


def test_packet_carries_phase_and_signature_tracks_it():
    clock = SeededClock("2026-10-03T10:00:00")
    now = clock.now()
    results = [
        SourceResult("calendar:primary", "ok", count=1, items=[{
            "ref": "cal:primary:cal:e1", "id": "e1", "title": "Standup",
            "start": (now + timedelta(minutes=10)).isoformat(), "all_day": False,
            "status": "confirmed", "source": "primary",
        }], checked_at=now.isoformat()),
    ]
    packet = build_packet(results, clock=clock, policy=_policy(), baseline={})
    cand = packet["candidates"][0]
    assert cand["phase"] == "within30min"
    moved = {**cand, "phase": "passed"}
    assert candidate_signature(cand) != candidate_signature(moved)


# -- stable digest ------------------------------------------------------------


def _one_result(checked_at, subject="Standup"):
    return SourceResult("calendar:primary", "ok", count=1, items=[{
        "ref": "cal:primary:cal:e1", "id": "e1", "title": subject,
        "start": "2026-10-03T11:00:00+08:00", "all_day": False,
        "status": "confirmed", "source": "primary",
    }], checked_at=checked_at)


def test_digest_excludes_checked_and_generated_at():
    c1 = SeededClock("2026-10-03T10:00:00")
    c2 = SeededClock("2026-10-03T10:00:05")
    p1 = build_packet([_one_result(c1.now().isoformat())], clock=c1, policy=_policy(), baseline={})
    p2 = build_packet([_one_result(c2.now().isoformat())], clock=c2, policy=_policy(), baseline={})
    assert packet_digest(p1) == packet_digest(p2)


def test_digest_tracks_version_phase_and_health():
    c1 = SeededClock("2026-10-03T10:00:00")
    base = build_packet([_one_result(c1.now().isoformat())], clock=c1, policy=_policy(), baseline={})
    changed = build_packet([_one_result(c1.now().isoformat(), subject="Standup (moved)")],
                           clock=c1, policy=_policy(), baseline={})
    assert packet_digest(base) != packet_digest(changed)

    truncated = build_packet([SourceResult("mail:formal", "ok", count=15, items=[], truncated=True,
                                           checked_at=c1.now().isoformat())],
                             clock=c1, policy=_policy(), baseline={})
    healthy = build_packet([SourceResult("mail:formal", "ok", count=15, items=[],
                                         checked_at=c1.now().isoformat())],
                           clock=c1, policy=_policy(), baseline={})
    assert packet_digest(truncated) != packet_digest(healthy)

    later = SeededClock("2026-10-03T12:00:00")
    phase_changed = build_packet([_one_result(later.now().isoformat())], clock=later,
                                 policy=_policy(), baseline={})
    assert packet_digest(base) != packet_digest(phase_changed)


# -- aliases + bounded last-DM context ----------------------------------------


def test_default_sources_covers_aliases_and_mail():
    factories = default_sources(_policy(), SeededClock("2026-10-03T10:00:00"))
    expected = 2 * len(_google_aliases()) + len(ALLOWED_MAIL_ACCOUNTS)
    assert len(factories) == expected
    assert "primary" in _google_aliases()


def test_last_dm_context_is_bounded_and_opt_in():
    assert collect_last_dm_preferences() == []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "session.json"
        path.write_text(
            "please stop sending newsletter mail\n"
            "an unrelated long chat about the weather and lunch plans\n"
            "remind me about the dentist\n" + "x" * 500 + "\n",
            encoding="utf-8",
        )
        found = collect_last_dm_preferences(sessions_dir=tmp, max_lines=2, max_chars=80)
    assert len(found) == 2
    assert all(len(line) <= 80 for line in found)
    assert any("stop sending" in line for line in found)
    assert not any("weather" in line for line in found)


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
    print(f"\n{passed}/{len(tests)} source-completion tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
