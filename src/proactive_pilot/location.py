"""Stage-only, local/ephemeral Find Hub phone context.

This module adds *conditional* phone context to the proactive pilot: the user's
own phone is only consulted around an upcoming meaningful in-person calendar
event, and only when local-only mode is explicitly opted in via per-pilot
policy. It is staged and privacy-first by construction.

Hard privacy invariants (enforced here, asserted by the tests):

- Coordinates, addresses and raw Find Hub responses are LOCAL/EPHEMERAL. They are
  never returned, logged, persisted, placed in the packet, or placed in an error
  string. Only derived, minimal facts (``available``/``fresh`` and an approximate
  straight-line distance bucket) may leave the module.
- The local origin and the phone device id are FIXED constants. No URL is ever
  taken from a source or from event text; no other tracked object is queried.
- Eligibility requires a POSITIVE physical venue or a valid explicit GEO. Missing
  venue, TBD/TBA, an ambiguous location, an online-only event, an all-day or a
  cancelled event yield ZERO phone HTTP. A generic web URL in an event
  description alone is *not* treated as evidence the event is remote.
- No external route queries, no geocoding, no travel-time/route ETA, no location
  history, no retention of current position across runs.
- No network is performed by tests or the worker. The production transport is a
  fixed-origin loopback client with redirects disabled and proxy/env trust off;
  tests always inject an explicit fake responder.
- Fail closed on null/malformed/stale/missing-time/date-only/naive/NaN/
  out-of-bounds/bad-accuracy/boolean/identity-missing/wrong-device/status-error.

A global pause suppresses every read. Quiet hours suppress discretionary reads.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from .policy import active_snooze, in_quiet_hours

# -- Fixed local origin / allowlisted phone ------------------------------------

LOCAL_ORIGIN = "http://127.0.0.1:8000"
PHONE_DEVICE_ID = "69bbb7d2-0000-2cef-8f87-089e082f6890"
PHONE_PATH = f"/api/v1/devices/{PHONE_DEVICE_ID}"
FORCE_URL = f"{LOCAL_ORIGIN}{PHONE_PATH}?force=true"

# Bounds (kept inside the global collector ceiling of 60 s).
MAX_BODY_BYTES = 64 * 1024
HTTP_DEADLINE_SECONDS = 5.0
MIN_BUDGET_SECONDS = 1.5
EVENT_WINDOW_MINUTES = 90
FORCE_CADENCE_MINUTES = 15
FRESH_MAX_SECONDS = 300
ACCURACY_MAX_METERS = 200.0
DISTANCE_BUCKET_METERS = 100
CLOCK_SKEW_SECONDS = 60

DISCLAIMER = "Approximate straight-line distance, not a route ETA."

# The only persisted cadence metadata: a last-attempt timestamp per phone.
LOCATION_ATTEMPTS_KEY = "location_attempts"

_CANCELLED_STATUSES = {"cancelled", "canceled", "declined"}
# Explicit online-provider markers. A bare generic URL is deliberately NOT a
# marker (a campus event can carry a web link and still be in person).
_ONLINE_MARKERS = (
    "zoom", "meet.google", "google meet", "teams.microsoft", "microsoft teams",
    "webex", "webinar", "livestream", "live stream", "dial-in", "dial in",
    "video call", "phone call", "online only", "online-only", "remote only",
    "remote-only", "virtual event", "virtual meeting",
)
_AMBIGUOUS_VENUE_MARKERS = (
    "tbd", "tba", "to be determined", "to be announced", "n/a", "na",
    "unknown", "unspecified", "online", "remote", "virtual",
)


class HttpGetter(Protocol):
    """Injected fixed-origin HTTP responder (fake in every test)."""

    def __call__(self, url: str, *, timeout: float, max_bytes: int) -> "LocationHttpResponse": ...


@dataclass
class LocationHttpResponse:
    """A bounded HTTP response. ``error`` is a safe fixed code, never a body."""

    status: int = 0
    body: str = ""
    error: str = ""


@dataclass
class PhoneFix:
    """Derived, coordinate-free view of a phone fix. Internal to the module."""

    available: bool = False
    fresh: bool = False
    reason: str = "location unavailable"
    distance_m: int | None = None
    distance_confidence: str = ""
    destination_resolved: bool = False
    disclaimer: str = ""


@dataclass
class LocationContext:
    """Safe, serialisable phone context for the evidence packet.

    Contains only derived booleans/codes, the exact bound event ref and an
    approximate distance bucket. Never latitude/longitude/address/raw response.
    """

    status: str = "disabled"
    available: bool = False
    fresh: bool = False
    event_ref: str = ""
    distance_m: int | None = None
    distance_confidence: str = ""
    destination_resolved: bool = False
    disclaimer: str = ""
    forced: bool = False
    attempted: bool = False

    def to_packet(self) -> dict[str, Any]:
        """Minimal packet/state-safe projection (no coordinates, no titles)."""
        payload: dict[str, Any] = {
            "source": "phone_location",
            "status": str(self.status),
            "available": bool(self.available),
            "fresh": bool(self.fresh),
            "destination_resolved": bool(self.destination_resolved),
            "forced": bool(self.forced),
            "ephemeral": True,
        }
        if self.event_ref:
            payload["ref"] = str(self.event_ref)
        if self.distance_m is not None:
            payload["distance_approx_m"] = int(self.distance_m)
            payload["distance_confidence"] = str(self.distance_confidence or "approximate")
            payload["disclaimer"] = DISCLAIMER
        return payload


# -- Production fixed-origin transport (never used by the worker/tests) --------


class _NoRedirectHandler:
    """A minimal redirect handler factory that refuses every redirect.

    Returning ``None`` from ``redirect_request`` makes urllib treat a 3xx as an
    unhandled HTTP error instead of following it to another origin. Defined as a
    factory so importing urllib stays lazy and the class is testable offline.
    """

    @staticmethod
    def build():
        import urllib.request

        class _Handler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D401
                return None

        return _Handler()


class LocalLoopbackTransport:
    """Fixed-origin, no-redirect, no-proxy HTTP client for the local service.

    Only the exact allowlisted phone URL is ever requested. Environment proxies
    are ignored (empty ``ProxyHandler``) and redirects are refused, so a
    compromised local service cannot bounce the request to an external host.
    Tests must inject a fake instead of using this class.
    """

    def __init__(self) -> None:
        import urllib.request

        self._opener = urllib.request.build_opener(
            _NoRedirectHandler.build(), urllib.request.ProxyHandler({}),
        )

    @staticmethod
    def allowed(url: str) -> bool:
        return str(url) == FORCE_URL

    def __call__(self, url: str, *, timeout: float, max_bytes: int) -> LocationHttpResponse:
        if not self.allowed(url):
            return LocationHttpResponse(status=0, body="", error="origin not allowed")
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        try:
            from .collectors import wallclock_alarm
            with wallclock_alarm(float(timeout)):
                with self._opener.open(request, timeout=max(0.2, float(timeout))) as response:
                    body = response.read(int(max_bytes) + 1)
        except urllib.error.HTTPError as exc:
            return LocationHttpResponse(status=int(getattr(exc, "code", 0) or 0), body="", error="http error")
        except Exception:  # noqa: BLE001 - never surface raw exception text (may carry coords)
            return LocationHttpResponse(status=0, body="", error="fetch failed")
        if len(body) > int(max_bytes):
            return LocationHttpResponse(status=0, body="", error="location oversize")
        return LocationHttpResponse(status=200, body=body.decode("utf-8", "replace"), error="")


# -- Time + event qualification ------------------------------------------------


def _parse_time(value: Any, now: datetime) -> datetime | None:
    text = str(value or "").strip()
    if len(text) == 10:  # date-only: not an exact time
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed


def _parse_timestamp_strict(value: Any) -> datetime | None:
    """Phone timestamps must be tz-aware and carry an exact time (no date-only)."""
    text = str(value or "").strip()
    if not text or len(text) == 10:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _is_online(item: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(item.get(field, "")) for field in ("title", "subject", "location", "venue", "description")
    ).lower()
    return any(marker in haystack for marker in _ONLINE_MARKERS)


def _is_url(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    return lowered.startswith(("http://", "https://", "www.")) or "://" in lowered


def _physical_venue(item: dict[str, Any]) -> str:
    """A positive physical venue string, or '' when none/ambiguous/URL."""
    for key in ("location", "venue"):
        raw = str(item.get(key) or "").strip()
        if not raw:
            continue
        lowered = raw.lower().strip(" .")
        if lowered in _AMBIGUOUS_VENUE_MARKERS:
            continue
        if _is_url(raw):
            continue
        return raw
    return ""


def _has_explicit_geo(item: dict[str, Any]) -> bool:
    return _coords(item.get("geo")) is not None or _coords(item.get("location_geo")) is not None


def _is_ambiguous(item: dict[str, Any], now: datetime) -> bool:
    title = str(item.get("title") or item.get("subject") or "").strip()
    if not title or title == "(untitled event)":
        return True
    if any(marker in title.lower() for marker in _AMBIGUOUS_VENUE_MARKERS):
        return True
    return _parse_time(item.get("start") or item.get("date"), now) is None


def qualifying_events(
    results: list[Any],
    now: datetime,
    *,
    window_minutes: int = EVENT_WINDOW_MINUTES,
) -> list[dict[str, Any]] | None:
    """Upcoming meaningful in-person events within the pre-event window.

    Returns ``None`` when NO healthy calendar source is present (calendar
    unavailable -> the caller must perform no phone read), or a sorted
    (earliest-first) possibly-empty list when calendar coverage is healthy.

    Eligibility requires a positive physical venue OR a valid explicit GEO.
    Excludes cancelled, all-day, online-only, ambiguous and missing-venue events.
    Titles are never returned separately; the raw item stays ephemeral.
    """
    calendar_results = [r for r in results if str(getattr(r, "name", "")).startswith("calendar:")]
    healthy = [r for r in calendar_results if bool(getattr(r, "healthy", False))]
    if not healthy:
        return None
    window = timedelta(minutes=max(1, int(window_minutes)))
    out: list[dict[str, Any]] = []
    for result in healthy:
        for item in getattr(result, "items", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "confirmed").lower() in _CANCELLED_STATUSES:
                continue
            if bool(item.get("all_day")):
                continue
            if _is_online(item):
                continue
            if _is_ambiguous(item, now):
                continue
            if not (_physical_venue(item) or _has_explicit_geo(item)):
                continue
            start = _parse_time(item.get("start") or item.get("date"), now)
            if start is None:
                continue
            if not (now < start <= now + window):
                continue
            out.append(item)
    out.sort(key=lambda it: _parse_time(it.get("start") or it.get("date"), now) or now)
    return out


# -- Local destination resolution (explicit coordinates only) ------------------


def _coords(value: Any) -> tuple[float, float] | None:
    """Parse an explicit coordinate pair; never invents/geocodes a name."""
    if isinstance(value, dict):
        lat = value.get("latitude", value.get("lat"))
        lon = value.get("longitude", value.get("lon", value.get("lng")))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        lat, lon = value[0], value[1]
    else:
        lat = lon = None
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    try:
        lat_f, lon_f = float(lat), float(lon)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
        return None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return None
    return lat_f, lon_f


def _parse_coord_text(text: str) -> tuple[float, float] | None:
    parts = str(text or "").split(",")
    if len(parts) != 2:
        return None
    return _coords((parts[0].strip(), parts[1].strip()))


def resolve_destination(
    event: dict[str, Any] | None,
    explicit: tuple[float, float] | None = None,
) -> tuple[tuple[float, float] | None, bool]:
    """Resolve a destination ONLY from explicit local coordinates.

    Considers the single bound (earliest) event and an explicit caller override.
    A named venue is never geocoded: an unknown named destination stays
    unresolved (``(None, False)``). Returns ``(coords, resolved)``.
    """
    if explicit is not None:
        parsed = _coords(explicit)
        if parsed is not None:
            return parsed, True
    if not isinstance(event, dict):
        return None, False
    for key in ("geo", "location_geo"):
        parsed = _coords(event.get(key))
        if parsed is not None:
            return parsed, True
    parsed = _parse_coord_text(str(event.get("location") or ""))
    if parsed is not None:
        return parsed, True
    return None, False


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Offline great-circle distance in metres (no route, no network)."""
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def _bucket_distance(distance_m: float) -> int:
    return int(round(distance_m / DISTANCE_BUCKET_METERS) * DISTANCE_BUCKET_METERS)


# -- Response parsing (fail closed, coordinate-free) ---------------------------


def parse_device_fix(
    payload: Any,
    *,
    now: datetime,
    destination: tuple[float, float] | None = None,
    destination_resolved: bool = False,
) -> PhoneFix:
    """Validate a device payload. Any uncertainty -> unavailable (fail closed).

    Strict identity: the actual bridge carries the exact allowlisted device_id.
    Legacy id payloads additionally require is_own_device True; conflicting fields
    are rejected. Coordinates/accuracy must be real finite numbers
    (booleans rejected) inside valid ranges; the timestamp must be tz-aware,
    not date-only, not naive, not in the future and not stale. The only values
    that escape are ``available``/``fresh`` and an approximate distance bucket.
    """
    if not isinstance(payload, dict):
        return PhoneFix(reason="location parse error")
    if "device_id" in payload:
        # Actual local bridge OpenAPI uses device_id, not id/is_own_device.
        # The fixed user-approved phone identifier remains mandatory.
        if str(payload.get("device_id")) != PHONE_DEVICE_ID:
            return PhoneFix(reason="wrong device")
        if "id" in payload and str(payload.get("id")) != PHONE_DEVICE_ID:
            return PhoneFix(reason="wrong device")
        if "is_own_device" in payload and payload.get("is_own_device") is not True:
            return PhoneFix(reason="not own device")
    else:
        if "id" not in payload or payload.get("id") is None:
            return PhoneFix(reason="missing identity")
        if str(payload.get("id")) != PHONE_DEVICE_ID:
            return PhoneFix(reason="wrong device")
        if payload.get("is_own_device") is not True:
            return PhoneFix(reason="not own device")
    location = payload.get("location")
    if not isinstance(location, dict):
        return PhoneFix(reason="location unavailable")

    lat = location.get("latitude", location.get("lat"))
    lon = location.get("longitude", location.get("lon", location.get("lng")))
    acc = location.get("accuracy")
    if isinstance(lat, bool) or isinstance(lon, bool) or isinstance(acc, bool):
        return PhoneFix(reason="location parse error")
    try:
        lat_f, lon_f = float(lat), float(lon)  # type: ignore[arg-type]
        accuracy = float(acc)
    except (TypeError, ValueError):
        return PhoneFix(reason="location parse error")
    if not (math.isfinite(lat_f) and math.isfinite(lon_f) and math.isfinite(accuracy)):
        return PhoneFix(reason="location parse error")
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return PhoneFix(reason="location out of bounds")
    if accuracy < 0:
        return PhoneFix(reason="location parse error")
    if accuracy > ACCURACY_MAX_METERS:
        return PhoneFix(reason="location bad accuracy")

    stamp = _parse_timestamp_strict(location.get("timestamp"))
    if stamp is None:
        return PhoneFix(reason="location missing time")
    if stamp > now + timedelta(seconds=CLOCK_SKEW_SECONDS):
        return PhoneFix(reason="location time future")
    age_seconds = (now - stamp).total_seconds()
    if age_seconds > FRESH_MAX_SECONDS:
        return PhoneFix(reason="location stale")

    distance: int | None = None
    if destination is not None and destination_resolved:
        distance = _bucket_distance(haversine_m(lat_f, lon_f, destination[0], destination[1]))
    return PhoneFix(
        available=True,
        fresh=True,
        reason="ok",
        distance_m=distance,
        distance_confidence="approximate" if distance is not None else "",
        destination_resolved=bool(destination_resolved),
        disclaimer=DISCLAIMER if distance is not None else "",
    )


# -- Cadence (last attempt timestamp only) -------------------------------------


def last_attempt(state: dict[str, Any] | None, phone_id: str = PHONE_DEVICE_ID) -> datetime | None:
    if not isinstance(state, dict):
        return None
    attempts = state.get(LOCATION_ATTEMPTS_KEY)
    if not isinstance(attempts, dict):
        return None
    raw = attempts.get(phone_id)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed


def record_attempt(state: dict[str, Any] | None, now: datetime, phone_id: str = PHONE_DEVICE_ID) -> None:
    """Persist ONLY a last-attempt timestamp. Never a position or a title."""
    if not isinstance(state, dict):
        return
    attempts = state.get(LOCATION_ATTEMPTS_KEY)
    if not isinstance(attempts, dict):
        attempts = {}
        state[LOCATION_ATTEMPTS_KEY] = attempts
    attempts[phone_id] = now.isoformat()


def cadence_allows(state: dict[str, Any] | None, now: datetime, *, cadence_minutes: int = FORCE_CADENCE_MINUTES) -> bool:
    previous = last_attempt(state)
    if previous is None:
        return True
    if previous.tzinfo is None and now.tzinfo is not None:
        previous = previous.replace(tzinfo=now.tzinfo)
    return now - previous >= timedelta(minutes=max(0, int(cadence_minutes)))


# -- Top-level collection ------------------------------------------------------


def collect_location_context(
    results: list[Any],
    *,
    clock: Any,
    policy: dict[str, Any],
    prefs: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    http_get: HttpGetter | None = None,
    destination: tuple[float, float] | None = None,
    deadline_seconds: float = HTTP_DEADLINE_SECONDS,
) -> LocationContext:
    """Conditionally fetch and derive local phone context.

    Performs NO HTTP when: local-only mode is not opted in, a global pause is
    active, it is quiet hours, calendar coverage is unavailable, no qualifying
    in-person event is upcoming, the force cadence is still cooling down, or the
    remaining budget is too small (deferred, not fabricated healthy).

    The caller must inject ``http_get``; the default is the production fixed
    loopback transport. Tests always inject an explicit fake.
    """
    if not policy.get("location_context_enabled", False):
        return LocationContext(status="disabled")

    now = clock.now()
    prefs = prefs or {}
    if active_snooze(prefs, now):
        return LocationContext(status="global pause")
    if in_quiet_hours(policy, now):
        return LocationContext(status="quiet hours")

    if float(deadline_seconds) < MIN_BUDGET_SECONDS:
        return LocationContext(status="budget deferred")

    events = qualifying_events(results, now, window_minutes=int(policy.get(
        "location_event_window_minutes", EVENT_WINDOW_MINUTES)))
    if events is None:
        return LocationContext(status="calendar unavailable")
    if not events:
        return LocationContext(status="no qualifying event")

    bound = events[0]
    event_ref = str(bound.get("ref") or "")

    cadence = int(policy.get("location_force_cadence_minutes", FORCE_CADENCE_MINUTES))
    if not cadence_allows(state, now, cadence_minutes=cadence):
        return LocationContext(status="cadence", event_ref=event_ref)

    dest_coords, dest_resolved = resolve_destination(bound, destination)
    timeout = max(0.2, min(float(deadline_seconds), HTTP_DEADLINE_SECONDS))
    getter = http_get if http_get is not None else LocalLoopbackTransport()
    try:
        response = getter(FORCE_URL, timeout=timeout, max_bytes=int(MAX_BODY_BYTES))
    except Exception:  # noqa: BLE001 - never surface raw exception text (may carry coords)
        response = LocationHttpResponse(status=0, error="location fetch failed")
    record_attempt(state, now)

    def fail(reason: str) -> LocationContext:
        return LocationContext(status=reason, event_ref=event_ref, forced=True, attempted=True)

    if not isinstance(response, LocationHttpResponse):
        return fail("location parse error")
    if response.error:
        return fail("location fetch failed")
    if int(response.status) != 200:
        return fail(f"location status {int(response.status)}")
    body = response.body or ""
    if len(body.encode("utf-8", errors="ignore")) > MAX_BODY_BYTES:
        return fail("location oversize")
    import json

    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return fail("location parse error")

    fix = parse_device_fix(payload, now=now, destination=dest_coords, destination_resolved=dest_resolved)
    return LocationContext(
        status="ok" if fix.available else fix.reason,
        available=fix.available,
        fresh=fix.fresh,
        event_ref=event_ref,
        distance_m=fix.distance_m,
        distance_confidence=fix.distance_confidence,
        destination_resolved=fix.destination_resolved,
        disclaimer=fix.disclaimer,
        forced=True,
        attempted=True,
    )
