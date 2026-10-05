"""Optional Google Workspace calendar adapter over the verified native skill CLI.

This is an OPTIONAL adapter, not an assumed universal registered-tool API. It
uses the already-installed ``productivity/google-workspace`` skill script
(``google_api.py``) with its existing authorised runtime authentication and the
active ``HERMES_HOME`` token. It does not vendor the skill, does not create an
alternate OAuth store, and does not read or refresh token values itself.

Contract (verified against the copied native ``google_api.py``):

    python google_api.py calendar list --start <ISO> --end <ISO> \
        --max <N> --calendar <calendarId>

which prints a JSON array of ``{id, summary, start, end, location, description,
status, htmlLink}`` where ``start``/``end`` are either a date or an ISO datetime
the native helper always normalises to include an absolute offset/Z.

Fail closed, naming the disabled capability, on: disabled-by-config, missing
skill, missing dependency, absent token, unsupported account routing, malformed
response, non-zero exit or timeout. Readiness probing never invokes a connector
API and never reads a token value; a source call only happens after the operator
explicitly enables the adapter.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

CAPABILITY = "gws_calendar"
SKILL_RELATIVE = Path("skills") / "productivity" / "google-workspace" / "scripts" / "google_api.py"
_SAFE_CALENDAR = re.compile(r"^[A-Za-z0-9._@:+/-]{1,128}$")
# The native helper routes through the active HERMES_HOME token only; it has no
# per-account parameter, so any explicit non-active account routing is refused.
_ACTIVE_ACCOUNT_ALIASES = frozenset({"", "primary", "me", "active", "default"})


class GwsUnsupported(RuntimeError):
    """The optional Google Workspace calendar capability is not usable."""


def _hermes_home(hermes_home: str | Path | None = None) -> Path:
    if hermes_home is not None:
        return Path(hermes_home)
    override = os.environ.get("HERMES_HOME")
    return Path(override) if override else Path.home() / ".hermes"


def _find_spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def readiness(
    *,
    hermes_home: str | Path | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Passive readiness probe: file presence + dependency importability only.

    Never opens the token file (only ``exists()``), never refreshes it and never
    performs a connector API call. Presence of a token is NOT proof of the
    required scope: enforcement happens on the first real call.
    """
    which = which or __import__("shutil").which
    home = _hermes_home(hermes_home)
    script = home / SKILL_RELATIVE
    backend = "gws" if which("gws") else ("python-client" if _find_spec("googleapiclient") else "")
    token_present = (home / "google_token.json").exists()
    if not script.is_file():
        return {"capability": CAPABILITY, "ready": False, "backend": backend,
                "skill": str(script), "token_present": token_present,
                "reason": "google-workspace skill not installed"}
    if not backend:
        return {"capability": CAPABILITY, "ready": False, "backend": "",
                "skill": str(script), "token_present": token_present,
                "reason": "neither the gws CLI nor the native Google Python client is available"}
    if not token_present:
        return {"capability": CAPABILITY, "ready": False, "backend": backend,
                "skill": str(script), "token_present": False,
                "reason": "no authorised Google token present in this HERMES_HOME"}
    return {"capability": CAPABILITY, "ready": True, "backend": backend,
            "skill": str(script), "token_present": True,
            "reason": "ready (scope is enforced on the first authorised read)"}


@dataclass
class GwsEvent:
    ref: str
    subject: str
    start: str
    end: str
    all_day: bool
    source: str
    location: str = ""
    status: str = ""

    def to_item(self) -> dict[str, Any]:
        return {
            "kind": "calendar",
            "ref": self.ref,
            "subject": self.subject,
            "date": self.start,
            "end": self.end,
            "all_day": self.all_day,
            "location": self.location,
            "status": self.status,
            "source": self.source,
        }


def _parse_bound(value: Any) -> tuple[str, bool]:
    """Return ``(iso_or_date, all_day)`` requiring an absolute offset for times."""
    if not isinstance(value, str) or not value.strip():
        raise GwsUnsupported("malformed calendar response: missing event time")
    text = value.strip()
    if "T" not in text:
        try:
            date.fromisoformat(text[:10])
        except ValueError as exc:
            raise GwsUnsupported("malformed calendar response: bad all-day date") from exc
        return text[:10], True
    normalized = text if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GwsUnsupported("malformed calendar response: bad event datetime") from exc
    if parsed.tzinfo is None:
        raise GwsUnsupported("malformed calendar response: event datetime lacks an offset")
    return parsed.isoformat(), False


def parse_calendar_events(payload: str, *, calendar_id: str) -> list[GwsEvent]:
    """Parse the native JSON contract, failing closed on any deviation."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise GwsUnsupported("malformed calendar response: not JSON") from exc
    if not isinstance(data, list):
        raise GwsUnsupported("malformed calendar response: expected a JSON array")
    events: list[GwsEvent] = []
    for row in data:
        if not isinstance(row, dict) or not row.get("id"):
            raise GwsUnsupported("malformed calendar response: event missing id")
        start, all_day = _parse_bound(row.get("start"))
        end, _ = _parse_bound(row.get("end"))
        events.append(GwsEvent(
            ref=f"gws:{calendar_id}:{row['id']}",
            subject=str(row.get("summary") or "(no title)")[:200],
            start=start,
            end=end,
            all_day=all_day,
            source=f"calendar:gws:{calendar_id}",
            location=str(row.get("location") or "")[:200],
            status=str(row.get("status") or "")[:40],
        ))
    return events


class GwsCalendarAdapter:
    """Bounded, read-only calendar adapter over the installed skill CLI."""

    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        *,
        hermes_home: str | Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self.policy = policy or {}
        self.hermes_home = _hermes_home(hermes_home)
        self._runner = runner
        self._which = which

    @property
    def enabled(self) -> bool:
        return bool(self.policy.get("gws_calendar_enabled", False))

    def readiness(self) -> dict[str, Any]:
        return readiness(hermes_home=self.hermes_home, which=self._which)

    def _run(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        if self._runner is not None:
            return self._runner(argv, timeout)
        env = copy.copy(os.environ)
        env["HERMES_HOME"] = str(self.hermes_home)
        return subprocess.run(argv, shell=False, capture_output=True, text=True,
                              timeout=timeout, check=False, env=env)

    def build_argv(self, start: str, end: str, max_items: int, calendar_id: str) -> list[str]:
        script = self.hermes_home / SKILL_RELATIVE
        return [sys.executable, str(script), "calendar", "list",
                "--start", str(start), "--end", str(end),
                "--max", str(int(max_items)), "--calendar", str(calendar_id)]

    def list_events(
        self,
        *,
        account: str = "",
        calendar_id: str = "primary",
        start: str,
        end: str,
        max_items: int = 20,
        timeout: float = 20.0,
    ) -> list[GwsEvent]:
        if not self.enabled:
            raise GwsUnsupported(
                f"capability {CAPABILITY!r} is disabled; enable adapters.gws_calendar")
        if account not in _ACTIVE_ACCOUNT_ALIASES:
            raise GwsUnsupported(
                f"capability {CAPABILITY!r} does not support account routing; the native "
                "helper always uses the active HERMES_HOME token")
        if not isinstance(calendar_id, str) or not _SAFE_CALENDAR.fullmatch(calendar_id):
            raise GwsUnsupported("invalid calendar id")
        state = self.readiness()
        if not state.get("ready"):
            raise GwsUnsupported(f"{CAPABILITY} unavailable: {state.get('reason')}")
        if not isinstance(start, str) or not isinstance(end, str) or not start or not end:
            raise GwsUnsupported("calendar start/end are required")
        max_items = max(1, min(int(max_items), 100))
        argv = self.build_argv(start, end, max_items, calendar_id)
        try:
            proc = self._run(argv, timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GwsUnsupported(f"capability {CAPABILITY!r} failed to start ({type(exc).__name__})") from exc
        if proc.returncode != 0:
            raise GwsUnsupported(
                f"capability {CAPABILITY!r} failed (exit {proc.returncode}); "
                "check authorisation/scope with the google-workspace skill")
        return parse_calendar_events(proc.stdout or "", calendar_id=calendar_id)

    def collect(self, clock: Any, policy: dict[str, Any],
                *, budget: Any = None) -> Any:
        """A ``default_sources`` factory: one ``SourceResult`` per configured calendar.

        Source calls only happen after the operator enabled the adapter and named
        explicit calendars; the returned result is a normal read-only calendar
        result so the ordinary packet gates apply.
        """
        from .collectors import SourceResult, safe_source_error

        calendars = policy.get("calendar_ids") or []
        calendar_id = str(calendars[0]) if calendars else "primary"
        limit = int(policy.get("max_calendar_items", 20))
        name = f"calendar:gws:{calendar_id}"
        checked = clock.now().isoformat()
        if budget is not None and budget.expired():
            return SourceResult(name, "ok", count=0, items=[], checked_at=checked,
                                truncated=True, failure="collector budget exhausted before this source")
        horizon = int(policy.get("calendar_horizon_hours", 48))
        now = clock.now()
        from datetime import timedelta
        start = now.isoformat()
        end = (now + timedelta(hours=horizon)).isoformat()
        try:
            events = self.list_events(calendar_id=calendar_id, start=start, end=end,
                                      max_items=limit,
                                      timeout=float(policy.get("feed_timeout_seconds", 15)))
        except GwsUnsupported as exc:
            return SourceResult(name, "error", error=str(exc), checked_at=checked)
        except Exception as exc:  # noqa: BLE001
            return SourceResult(name, "error", error=safe_source_error(exc, kind="calendar"),
                                checked_at=checked)
        items = [event.to_item() for event in events]
        return SourceResult(name, "ok", count=len(items), items=items, checked_at=checked,
                            truncated=len(items) >= limit)


def readiness_report(policy: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Capability report for doctor/status, naming a disabled adapter honestly."""
    state = readiness(**kwargs)
    state["enabled_by_config"] = bool((policy or {}).get("gws_calendar_enabled", False))
    if not state["ready"] and state["enabled_by_config"]:
        state["reason"] = f"configured but unavailable: {state['reason']}"
    return state


__all__ = [
    "CAPABILITY", "SKILL_RELATIVE", "GwsUnsupported", "GwsEvent", "GwsCalendarAdapter",
    "readiness", "readiness_report", "parse_calendar_events",
]
