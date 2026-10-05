"""Timezone-safe clock. All proactive window math is in Asia/Singapore."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SG = ZoneInfo("Asia/Singapore")


class Clock:
    """Real wall clock in Asia/Singapore."""

    def now(self) -> datetime:
        return datetime.now(SG)

    def utc(self) -> datetime:
        return datetime.now(timezone.utc)


class SeededClock:
    """Deterministic clock for fixtures; accepts an aware or naive ISO string.

    A naive string is interpreted as Asia/Singapore local time so date-only
    semantics and quiet-hour gates are exercised against the real timezone.
    """

    def __init__(self, iso: str):
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SG)
        self._now = parsed

    def now(self) -> datetime:
        return self._now

    def utc(self) -> datetime:
        return self._now.astimezone(timezone.utc)
