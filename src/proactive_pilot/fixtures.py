"""Synthetic fixtures for deterministic tests. No production data is touched."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from .collectors import SourceResult
from .evaluator import EvaluatorResult, validate_object


def fixture_results(name: str, clock: Any) -> list[SourceResult]:
    now = clock.now().isoformat()
    if name == "healthy_empty":
        return [
            SourceResult("calendar:primary", "ok", count=0, items=[], checked_at=now),
            SourceResult("tasks:primary", "ok", count=0, items=[], checked_at=now),
            SourceResult("mail:formal", "ok", count=0, items=[], checked_at=now),
            SourceResult("mail:informal", "ok", count=0, items=[], checked_at=now),
        ]
    if name == "failure":
        return [
            SourceResult("calendar:primary", "error", error="HTTP 503", checked_at=now),
            SourceResult("tasks:primary", "ok", count=0, items=[], checked_at=now),
            SourceResult("mail:formal", "error", error="timeout", checked_at=now),
            SourceResult("mail:informal", "ok", count=0, items=[], checked_at=now),
        ]
    if name == "urgent_calendar":
        # A genuinely near-term, source-backed urgency: the concrete start time
        # is inside the urgent bound relative to the seeded clock.
        start = (clock.now() + timedelta(minutes=45)).isoformat()
        return [
            SourceResult("calendar:primary", "ok", count=1, items=[{
                "ref": "evt:primary:u1", "id": "u1", "title": "Dentist appointment (moved)",
                "start": start, "all_day": False,
                "updated": "2026-10-02T12:00:00Z", "source": "primary",
            }], checked_at=now),
        ]
    if name == "quiet_invalid_exception":
        # model claims urgent but supplies NO evidence -> must be rejected
        return [
            SourceResult("calendar:primary", "ok", count=1, items=[{
                "ref": "evt:primary:q1", "id": "q1", "title": "Study block",
                "start": "2026-10-02T03:00:00+08:00", "all_day": False,
                "updated": "", "source": "primary",
            }], checked_at=now),
        ]
    if name == "changed_event":
        return [
            SourceResult("calendar:primary", "ok", count=1, items=[{
                "ref": "evt:primary:c1", "id": "c1", "title": "Group meeting (time changed)",
                "start": "2026-10-02T15:00:00+08:00", "all_day": False,
                "updated": "2026-10-02T13:00:00Z", "source": "primary",
            }], checked_at=now),
        ]
    if name == "location_inperson":
        # In-person event inside the pre-event window WITH a physical venue:
        # eligible for the opt-in local phone-context pass.
        start = (clock.now() + timedelta(minutes=45)).isoformat()
        return [
            SourceResult("calendar:primary", "ok", count=1, items=[{
                "ref": "evt:primary:loc1", "id": "loc1",
                "title": "Dentist appointment", "start": start, "all_day": False,
                "status": "confirmed", "calendar": "primary",
                "location": "Clinic, 12 Orchard Road",
                "updated": "2026-10-02T12:00:00Z", "source": "primary",
            }], checked_at=now),
            SourceResult("tasks:primary", "ok", count=0, items=[], checked_at=now),
            SourceResult("mail:formal", "ok", count=0, items=[], checked_at=now),
            SourceResult("mail:informal", "ok", count=0, items=[], checked_at=now),
        ]
    if name == "location_online":
        # Online-only event inside the window: NOT eligible -> zero phone HTTP.
        start = (clock.now() + timedelta(minutes=30)).isoformat()
        return [
            SourceResult("calendar:primary", "ok", count=1, items=[{
                "ref": "evt:primary:onl1", "id": "onl1",
                "title": "Remote sync", "start": start, "all_day": False,
                "status": "confirmed", "calendar": "primary",
                "location": "https://meet.google.com/abc-defg",
                "updated": "2026-10-02T12:00:00Z", "source": "primary",
            }], checked_at=now),
            SourceResult("tasks:primary", "ok", count=0, items=[], checked_at=now),
            SourceResult("mail:formal", "ok", count=0, items=[], checked_at=now),
            SourceResult("mail:informal", "ok", count=0, items=[], checked_at=now),
        ]
    if name == "noise":
        return [
            SourceResult("mail:formal", "ok", count=1, items=[{
                "ref": "mail:formal:n1", "sender": "jobs-noreply@linkedin.com",
                "subject": "10 new jobs for you", "date": "2026-10-02T09:00:00Z",
                "has_attachment": False, "source": "formal",
            }], checked_at=now),
        ]
    return []


def fixture_evaluator(name: str) -> Any:
    class _Fake:
        def evaluate(self, packet: dict[str, Any], policy: dict[str, Any]) -> EvaluatorResult:
            if name == "urgent_calendar":
                cand = next((c for c in packet.get("candidates", []) if c.get("ref") == "evt:primary:u1"), {})
                return EvaluatorResult("notify", kind="calendar", key="evt:primary:u1",
                                       message="Your dentist appointment moved; it starts soon.",
                                       urgency="urgent", evidence=["evt:primary:u1"], expiry="",
                                       consequence="Missing the moved appointment slot",
                                       confidence="high", deadline=str(cand.get("date", "")),
                                       raw="{}")
            if name == "quiet_invalid_exception":
                # claims urgent, no evidence -> validate_object must reject
                obj = {"schema": 1, "action": "notify", "kind": "calendar", "key": "evt:primary:q1",
                       "message": "URGENT you are late", "urgency": "urgent", "expiry": "", "evidence": [],
                       "proposal": ""}
                return validate_object(obj, packet)
            if name == "changed_event":
                return EvaluatorResult("notify", kind="calendar", key="evt:primary:c1",
                                       message="Group meeting moved to 15:00 today.", urgency="normal",
                                       evidence=["evt:primary:c1"], raw="{}")
            if name == "noise":
                return EvaluatorResult("silence", reason="generic career noise", raw="{}")
            return EvaluatorResult("silence", reason="no change", raw="{}")
    return _Fake()
