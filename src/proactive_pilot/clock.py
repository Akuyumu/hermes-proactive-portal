"""Timezone-aware clock.

The real clock is constructed with the *configured* IANA timezone. Regional
adapters that are inherently Singapore-specific (for example LTA DataMall) keep
their own explicit ``Asia/Singapore`` semantics rather than following the user's
timezone.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SG = ZoneInfo("Asia/Singapore")
DEFAULT_TIMEZONE = "UTC"


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Return a valid ``ZoneInfo`` or raise ``ValueError``.

    A missing name uses the neutral UTC default so offline callers that have no
    policy never inherit an owner location. A present but unknown name fails
    closed instead of silently using the wrong local day.
    """
    text = str(name or "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {name!r}") from exc


class Clock:
    """Real wall clock in the configured timezone (neutral UTC default)."""

    def __init__(self, timezone_name: str | None = None):
        self.timezone = resolve_timezone(timezone_name)

    def now(self) -> datetime:
        return datetime.now(self.timezone)

    def utc(self) -> datetime:
        return datetime.now(timezone.utc)


class SeededClock:
    """Deterministic clock for fixtures; accepts an aware or naive ISO string.

    A naive string is interpreted as local time in the configured timezone. An
    aware value is preserved as-is unless an explicit timezone is supplied, in
    which case it is converted so every local-day gate sees the configured zone.
    A naive fixture string is interpreted in Asia/Singapore for backward
    compatibility with existing deterministic fixtures; production ``Clock()``
    uses neutral UTC unless the user configuration chooses a zone.
    """

    def __init__(self, iso: str, timezone_name: str | None = None):
        parsed = datetime.fromisoformat(iso)
        if timezone_name is not None:
            zone = resolve_timezone(timezone_name)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=zone)
            else:
                parsed = parsed.astimezone(zone)
            self.timezone = zone
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=SG)
            self.timezone = parsed.tzinfo or SG
        self._now = parsed

    def now(self) -> datetime:
        return self._now

    def utc(self) -> datetime:
        return self._now.astimezone(timezone.utc)


def build_clock(timezone_name: str | None = None, seeded_iso: str | None = None):
    """Build the runtime clock with one explicit effective timezone contract."""
    if seeded_iso:
        return SeededClock(seeded_iso, timezone_name=timezone_name)
    return Clock(timezone_name)
