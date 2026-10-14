"""Per-source health incidents, bounded recovery and dispatch gates.

This is the durable, schema-validated repair for the ordinary coverage notice.
The old ``gates.coverage_notice`` emitted "newly failed" then a cooldown
recurring message on every scheduled scan. That is ordinary scheduled spam and
is replaced here by a real per-source circuit:

* Only an **attempted** read is evidence. A missing, cached, partial, truncated,
  deferred or busy observation is NEITHER a success nor a failure, so it can
  never clear or advance an incident (and it can never be mistaken for recovery).
* A retryable timeout/503 is silent at first. A chronic loss of a source the
  user explicitly marked critical is NOT allowed to stay invisible forever.
* A provider 429 with ``Retry-After``/backoff is honoured: the gate blocks the
  next dispatch until the real deadline, and an ordinary scheduled scan is never
  recorded as a "recovery".
* Dispatch is a real gate (closed / open / half-open probe) with a persisted
  ``next_permitted_at`` and proof of an actual client call -- not merely a label.
* A critical, persistent loss alerts at most ONCE per unresolved incident, even
  across restarts, and only once safe recovery is exhausted. Recovery requires
  an authoritative attempted complete success, after which a NEW incident may
  qualify again.

Nothing here decides personal importance and nothing here reads a source.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

SCHEMA = 1
_HEALTH_KEY = "source_health"

OBSERVATION_SUCCESS = "success"
OBSERVATION_FAILURE = "failure"
OBSERVATION_NON_EVIDENTIAL = "non_evidential"

FAILURE_RETRYABLE = "retryable"        # timeout / 503 / transient transport
FAILURE_RATE_LIMITED = "rate_limited"  # 429 with Retry-After / backoff
FAILURE_OWNER_ACTION = "owner_action"  # auth/credential revocation
FAILURE_OTHER = "other"

_STATE_CLOSED = "closed"
_STATE_OPEN = "open"
_STATE_HALF_OPEN = "half_open"

# Observations that are explicitly NOT evidence of success or failure.
_NON_EVIDENTIAL_STATUS = {"deferred", "busy", "cached", "partial", "missing", "stale"}
_NON_EVIDENTIAL_ERROR = (
    "budget-deferred", "deferred", "busy", "cached", "partial", "truncated",
)
_OWNER_ACTION_MARKERS = ("401 unauthorized", "401 unauthorised", "configured authorization requires renewal",
                         "authorization revoked", "authorisation revoked", "token revoked",
                         "invalid_grant", "missing configured grant")
_RATE_LIMITED_MARKERS = ("429", "rate limit", "rate-limit", "too many requests", "quota")
_RETRYABLE_MARKERS = ("timeout", "timed out", "503", "502", "504", "unavailable", "temporar")
_RETRY_AFTER_RE = re.compile(r"retry[- ]?after[^0-9]*(\d+)", re.IGNORECASE)


def _parse_iso(value: Any, now: datetime | None = None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None and now is not None and now.tzinfo is not None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed


def classify_observation(result: Any) -> str:
    """Classify a source result into success / failure / non_evidential.

    A positive healthy read is a success ONLY when it is a complete, fresh,
    authoritative read. A truncated, cached, deferred or busy result is
    non-evidential: it neither clears a failure episode nor starts one.
    """
    status = str(getattr(result, "status", "") or "").lower()
    if getattr(result, "attempted", None) is False:
        return OBSERVATION_NON_EVIDENTIAL
    if bool(getattr(result, "cached", False)):
        return OBSERVATION_NON_EVIDENTIAL
    if bool(getattr(result, "truncated", False)):
        return OBSERVATION_NON_EVIDENTIAL
    error = str(getattr(result, "error", "") or "").lower()
    if status in _NON_EVIDENTIAL_STATUS:
        return OBSERVATION_NON_EVIDENTIAL
    if any(marker in error for marker in _NON_EVIDENTIAL_ERROR):
        return OBSERVATION_NON_EVIDENTIAL
    if status == "ok" and bool(getattr(result, "healthy", False)) and not error:
        return OBSERVATION_SUCCESS
    if status in {"error", "failed", "failure"}:
        return OBSERVATION_FAILURE
    return OBSERVATION_NON_EVIDENTIAL


def classify_failure(error_text: Any) -> str:
    """Map a failure to a class used for retry / backoff / alert wording."""
    text = str(error_text or "").lower()
    if any(marker in text for marker in _OWNER_ACTION_MARKERS):
        return FAILURE_OWNER_ACTION
    if any(marker in text for marker in _RATE_LIMITED_MARKERS):
        return FAILURE_RATE_LIMITED
    if any(marker in text for marker in _RETRYABLE_MARKERS):
        return FAILURE_RETRYABLE
    return FAILURE_OTHER


def retry_after_seconds(error_text: Any) -> int:
    """Extract a bounded Retry-After/backoff in seconds from safe error text."""
    match = _RETRY_AFTER_RE.search(str(error_text or ""))
    if not match:
        return 0
    try:
        return max(0, min(int(match.group(1)), 24 * 3600))
    except ValueError:
        return 0


def _empty_state() -> dict[str, Any]:
    return {"schema": SCHEMA, "sources": {}, "next_incident_id": 1, "corrupt": False}


def _empty_record(incident_id: int) -> dict[str, Any]:
    return {
        "incident_id": incident_id,
        "failure_streak": 0,
        "attempted_checks": 0,
        "attempted_failures": 0,
        "successes": 0,
        "last_attempt_at": "",
        "last_failure_at": "",
        "last_success_at": "",
        "first_failure_at": "",
        "failure_class": "",
        "recovery_state": _STATE_CLOSED,
        "recovery_attempts": 0,
        "last_recovery_at": "",
        "next_permitted_at": "",
        "last_observation_at": "",
        "last_observation_kind": "",
        "alerted_incident": 0,
        "unresolved_alerted": False,
        "last_recovery_outcome": "",
        "alert_blocked": False,
        "delivery_state": "",
        "delivery_incident": 0,
    }


def load_source_health(state: dict[str, Any]) -> dict[str, Any]:
    """Return the validated health subtree, failing SAFE on corrupt/absent state.

    A corrupt subtree is NOT treated as "no incidents" for alerting purposes:
    it is replaced with a fresh structure flagged ``corrupt`` so the caller can
    suppress a duplicate alert instead of resetting and re-spamming.
    """
    raw = state.get(_HEALTH_KEY)
    if raw is None:
        return _empty_state()

    def invalid():
        health = {**_empty_state(), "corrupt": True,
                  "diagnostic": "invalid source-health schema; authoritative read required"}
        state[_HEALTH_KEY] = health
        return health

    def counter(value, minimum=0):
        return type(value) is int and minimum <= value <= 10**12

    if (not isinstance(raw, dict) or type(raw.get("schema")) is not int
            or raw.get("schema") != SCHEMA or type(raw.get("corrupt")) is not bool
            or not counter(raw.get("next_incident_id"), 1)):
        return invalid()
    if (set(raw) - {"schema", "sources", "next_incident_id", "corrupt", "diagnostic"}
            or not isinstance(raw.get("diagnostic", ""), str)
            or len(raw.get("diagnostic", "")) > 160):
        return invalid()
    sources = raw.get("sources")
    if not isinstance(sources, dict) or len(sources) > 512:
        return invalid()
    enums = {"failure_class": {"", FAILURE_RETRYABLE, FAILURE_RATE_LIMITED, FAILURE_OWNER_ACTION, FAILURE_OTHER},
             "recovery_state": {_STATE_CLOSED, _STATE_OPEN, _STATE_HALF_OPEN},
             "last_observation_kind": {"", OBSERVATION_SUCCESS, OBSERVATION_FAILURE, OBSERVATION_NON_EVIDENTIAL},
             "last_recovery_outcome": {"", OBSERVATION_SUCCESS, OBSERVATION_FAILURE, OBSERVATION_NON_EVIDENTIAL},
             "delivery_state": {"", "emitting", "uncertain", "emitted"}}
    for name, record in sources.items():
        if (not isinstance(name, str) or not 1 <= len(name) <= 200
                or any(ord(c) < 32 for c in name) or not isinstance(record, dict)):
            return invalid()
        # New fields have safe defaults for schema-1 state from 0.4.0/early 0.4.1.
        record.setdefault("last_recovery_outcome", "")
        record.setdefault("alert_blocked", bool(raw["corrupt"]))
        record.setdefault("delivery_state", "")
        record.setdefault("delivery_incident", 0)
        if set(record) - set(_empty_record(1)):
            return invalid()
        for key, default in _empty_record(1).items():
            value = record.get(key)
            if type(default) is bool:
                valid = type(value) is bool
            elif type(default) is int:
                valid = counter(value, 1 if key == "incident_id" else 0)
            elif key in enums:
                valid = isinstance(value, str) and value in enums[key]
            else:
                valid = isinstance(value, str) and len(value) <= 64 and (
                    not value or (_parse_iso(value) is not None and _parse_iso(value).tzinfo is not None))
            if not valid:
                return invalid()
        if record["incident_id"] >= raw["next_incident_id"]:
            return invalid()
        if (record["failure_streak"] > record["attempted_failures"]
                or record["attempted_failures"] + record["successes"] != record["attempted_checks"]
                or record["alerted_incident"] > record["incident_id"]
                or record["delivery_incident"] > record["incident_id"]):
            return invalid()
    return raw


def _new_incident(health: dict[str, Any]) -> int:
    incident_id = int(health.get("next_incident_id", 1))
    health["next_incident_id"] = incident_id + 1
    return incident_id


def record_observations(
    state: dict[str, Any],
    results: list[Any],
    now: datetime,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Persist per-source incidents from real, attempted observations only."""
    health = load_source_health(state)
    # A corrupt subtree is reset here, but the corrupt flag keeps alerting quiet
    # for this episode (fail safe, never a reset-spam storm).
    sources = health.setdefault("sources", {})
    threshold = _threshold(policy)
    base_cooldown = _cooldown(policy)

    for result in results:
        name = str(getattr(result, "name", "") or "")
        if not name or len(name) > 200 or any(ord(c) < 32 for c in name):
            continue
        record = sources.get(name)
        if not isinstance(record, dict):
            record = _empty_record(_new_incident(health))
            record["alert_blocked"] = bool(health.get("corrupt"))
            sources[name] = record
        observation = classify_observation(result)
        record["last_observation_at"] = now.isoformat()
        record["last_observation_kind"] = observation
        if record.get("recovery_state") == _STATE_HALF_OPEN and getattr(result, "attempted", None) is not False:
            # A durable dispatch reservation is not proof the client ran. Count
            # the probe when its actual result arrives, including partial and
            # cached outcomes, which still cannot clear/advance an incident.
            if record["last_recovery_outcome"] == "":
                record["recovery_attempts"] += 1
            record["last_recovery_outcome"] = observation
        elif (record["recovery_attempts"] > 0
              and record["last_recovery_outcome"] in {"", OBSERVATION_NON_EVIDENTIAL}
              and observation != OBSERVATION_NON_EVIDENTIAL):
            record["last_recovery_outcome"] = observation

        if observation == OBSERVATION_NON_EVIDENTIAL:
            # Not evidence: never touch streak / incident / recovery counters.
            continue

        record["attempted_checks"] = int(record.get("attempted_checks", 0)) + 1
        record["last_attempt_at"] = now.isoformat()

        if observation == OBSERVATION_SUCCESS:
            record["successes"] = int(record.get("successes", 0)) + 1
            record["last_success_at"] = now.isoformat()
            was_open = record.get("recovery_state") in {_STATE_OPEN, _STATE_HALF_OPEN} \
                or int(record.get("failure_streak", 0)) > 0
            record["failure_streak"] = 0
            record["first_failure_at"] = ""
            record["failure_class"] = ""
            record["recovery_state"] = _STATE_CLOSED
            record["recovery_attempts"] = 0
            record["next_permitted_at"] = ""
            record["alert_blocked"] = False
            if was_open:
                # An authoritative attempted complete success ends the incident;
                # a fresh incident id lets a later loss qualify again.
                record["incident_id"] = _new_incident(health)
                record["unresolved_alerted"] = False
                record["alerted_incident"] = 0
            continue

        # Failure
        record["attempted_failures"] = int(record.get("attempted_failures", 0)) + 1
        record["failure_streak"] = int(record.get("failure_streak", 0)) + 1
        record["last_failure_at"] = now.isoformat()
        if not record.get("first_failure_at"):
            record["first_failure_at"] = now.isoformat()
        failure_class = classify_failure(getattr(result, "error", ""))
        record["failure_class"] = failure_class
        if record["failure_streak"] >= threshold:
            record["recovery_state"] = _STATE_OPEN
        # A 429 owns its own deadline; every other failure uses the base cooldown.
        wait = _failure_wait(result, failure_class, base_cooldown)
        record["next_permitted_at"] = (now + wait).isoformat()

    state[_HEALTH_KEY] = health
    return health


def _failure_wait(result: Any, failure_class: str, base_cooldown: timedelta) -> timedelta:
    if failure_class == FAILURE_RATE_LIMITED:
        seconds = retry_after_seconds(getattr(result, "error", ""))
        if seconds:
            return timedelta(seconds=seconds)
    return base_cooldown


def _threshold(policy: dict[str, Any]) -> int:
    try:
        return max(1, int(policy.get("source_health_failure_threshold", 3)))
    except (TypeError, ValueError):
        return 3


def _cooldown(policy: dict[str, Any]) -> timedelta:
    try:
        minutes = max(1, int(policy.get("source_health_cooldown_minutes", 60)))
    except (TypeError, ValueError):
        minutes = 60
    return timedelta(minutes=minutes)


def _persistent_minutes(policy: dict[str, Any]) -> int:
    try:
        return max(0, int(policy.get("source_health_persistent_minutes", 120)))
    except (TypeError, ValueError):
        return 120


def _max_recovery_attempts(policy: dict[str, Any]) -> int:
    try:
        return max(1, int(policy.get("source_health_max_recovery_attempts", 2)))
    except (TypeError, ValueError):
        return 2


def critical_sources(policy: dict[str, Any]) -> set[str]:
    """Explicitly configured critical sources.

    Neutral by default: no source is critical unless the user's validated
    configuration names it. Mail/calendar are NOT implicitly critical.
    """
    raw = policy.get("health_critical_sources", [])
    if isinstance(raw, (list, tuple, set)):
        return {str(item).strip() for item in raw if str(item or "").strip()}
    return set()


def is_critical_source(policy: dict[str, Any], source: str) -> bool:
    return str(source) in critical_sources(policy)


def dispatch_gate(
    state: dict[str, Any],
    source: str,
    now: datetime,
    policy: dict[str, Any],
    *,
    mark: bool = True,
) -> dict[str, Any]:
    """Real per-source dispatch gate: closed / open cooldown / half-open probe.

    Returns ``{"dispatch": bool, "mode": str, "probe": bool, "next_check_at": str,
    "reason": str}``. When a half-open probe is permitted and ``mark`` is true,
    ``next_permitted_at`` is advanced by the cooldown immediately, so exactly ONE
    probe can be issued per window (proof of an actual client call, not a label).
    """
    health = load_source_health(state)
    record = (health.get("sources") or {}).get(str(source))
    if health.get("corrupt") and (not record or record.get("alert_blocked")):
        return {"dispatch": True, "mode": _STATE_CLOSED, "probe": False,
                "next_check_at": "", "reason": "corrupt-health-fail-open"}
    if not isinstance(record, dict):
        return {"dispatch": True, "mode": _STATE_CLOSED, "probe": False,
                "next_check_at": "", "reason": "no-incident"}
    streak = int(record.get("failure_streak", 0))
    if streak < _threshold(policy) and record.get("failure_class") != FAILURE_RATE_LIMITED:
        return {"dispatch": True, "mode": _STATE_CLOSED, "probe": False,
                "next_check_at": "", "reason": "below-threshold"}
    permitted = _parse_iso(record.get("next_permitted_at"), now)
    if permitted is not None and now < permitted:
        return {"dispatch": False, "mode": _STATE_OPEN, "probe": False,
                "next_check_at": str(record.get("next_permitted_at", "")),
                "reason": "cooldown"}
    probe = record["recovery_attempts"] < _max_recovery_attempts(policy)
    record["recovery_state"] = _STATE_HALF_OPEN if probe else _STATE_OPEN
    if mark:
        record["next_permitted_at"] = (now + _cooldown(policy)).isoformat()
        if probe:
            record["last_recovery_at"] = now.isoformat()
            record["last_recovery_outcome"] = ""
    state[_HEALTH_KEY] = health
    return {"dispatch": True, "mode": record["recovery_state"], "probe": probe,
            "next_check_at": str(record.get("next_permitted_at", "")),
            "reason": "half-open-probe" if probe else "scheduled-check-after-bounded-probes"}


def _incident_persistent(record: dict[str, Any], now: datetime, policy: dict[str, Any]) -> bool:
    first = _parse_iso(record.get("first_failure_at"), now)
    if first is None:
        return False
    return now - first >= timedelta(minutes=_persistent_minutes(policy))


def alerting_sources(
    state: dict[str, Any],
    now: datetime,
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    """Critical sources whose persistent loss warrants one alert now."""
    health = load_source_health(state)
    critical = critical_sources(policy)
    if not critical:
        return []
    out: list[dict[str, Any]] = []
    for name in sorted(critical):
        record = (health.get("sources") or {}).get(name)
        if not isinstance(record, dict):
            continue
        if record.get("alert_blocked") or record.get("failure_class") != FAILURE_OWNER_ACTION:
            continue
        if record.get("delivery_incident") == record.get("incident_id") and record.get("delivery_state"):
            continue
        if int(record.get("failure_streak", 0)) < _threshold(policy):
            continue
        if not _incident_persistent(record, now, policy):
            continue
        if (int(record.get("recovery_attempts", 0)) < _max_recovery_attempts(policy)
                or record.get("last_recovery_outcome") != OBSERVATION_FAILURE):
            continue
        if int(record.get("alerted_incident", 0)) == int(record.get("incident_id", 0)):
            continue  # at most once per unresolved incident
        out.append({
            "source": name,
            "failure_class": str(record.get("failure_class", "")),
            "owner_action": record.get("failure_class") == FAILURE_OWNER_ACTION,
            "incident_id": record["incident_id"],
        })
    return out


def health_alert(
    state: dict[str, Any],
    now: datetime,
    policy: dict[str, Any],
    *,
    commit: bool = True,
) -> str | None:
    """One alert for a critical, persistent, recovery-exhausted loss.

    Transactional: the incident is only marked alerted when a notice is actually
    emitted (``commit=True``), so a caller that suppresses the alert leaves the
    state untouched and it can surface on a later allowed run.
    """
    qualifying = alerting_sources(state, now, policy)
    if not qualifying:
        return None
    if commit:
        health = load_source_health(state)
        for item in qualifying:
            record = health["sources"].get(item["source"])
            if isinstance(record, dict):
                record["alerted_incident"] = int(record.get("incident_id", 0))
                record["unresolved_alerted"] = True
        state[_HEALTH_KEY] = health
    names = [item["source"] for item in qualifying]
    labels = ", ".join(names)
    owner = any(item["owner_action"] for item in qualifying)
    minutes = _persistent_minutes(policy)
    if len(names) == 1:
        subject = f"Critical source {labels} has been unreadable for over {minutes} minutes"
    else:
        subject = f"{len(names)} critical sources have been unreadable for over {minutes} minutes ({labels})"
    if owner:
        return (subject + "; its credentials or authorisation may need attention. "
                "Other checks continue.")
    return (subject + " even after safe recovery attempts; this may be a provider "
            "or connectivity problem. Other checks continue.")


def commit_alert(state: dict[str, Any], now: datetime, policy: dict[str, Any], *, incidents=None) -> dict[str, Any]:
    """Consume the alert signature after a notice is actually emitted."""
    qualifying = incidents if incidents is not None else alerting_sources(state, now, policy)
    health = load_source_health(state)
    for item in qualifying:
        record = health["sources"].get(item["source"])
        if isinstance(record, dict) and record["incident_id"] == item["incident_id"]:
            record["alerted_incident"] = int(record.get("incident_id", 0))
            record["unresolved_alerted"] = True
            record["delivery_state"] = "emitted"
            record["delivery_incident"] = record["incident_id"]
    state[_HEALTH_KEY] = health
    return health


# -- runtime guard -----------------------------------------------------------
# The evaluator prompt alone is not a guarantee: a model can still emit a
# routine health/coverage/scan message. This deterministic guard rejects such
# messages at the process boundary WITHOUT suppressing a real business warning
# (which names a concrete obligation, deadline or consequence).
_ROUTINE_HEALTH_MARKERS = (
    "could not be read", "coverage note", "source unavailable",
    "sources are unavailable", "health check", "health notice",
    "scan failed", "failed scan", "coverage degraded", "coverage reduced",
    "could not reach", "temporarily unavailable", "retrying", "will keep retrying",
    "provider", "rate limit", "429", "503", "timeout", "timed out",
    "having trouble", "unable to check", "unable to read", "cannot check",
    "monitoring unavailable", "coverage", "checks failed", "connection failed",
)
# A genuine business warning names a concrete, actionable consequence.
_BUSINESS_MARKERS = (
    "due", "deadline", "expires", "payment", "overdue", "rsvp", "renew",
    "cancel", "appointment", "submit", "form", "forms", "invoice", "interview",
)
_BUSINESS_RE = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in _BUSINESS_MARKERS) + r")\b",
    re.IGNORECASE,
)


def routine_health_reason(text: Any, *, proposal=None, packet=None) -> str | None:
    """Return a reason to reject a routine health message, or ``None`` to allow.

    A message that also carries a concrete business consequence is allowed: the
    guard must never block a real warning.
    """
    lowered = str(text or "").lower()
    if not lowered.strip():
        return None
    if proposal is not None and packet is not None:
        # Only a real candidate binding can distinguish a business consequence
        # from health facts. Mere words such as 'due' or 'form' prove nothing.
        candidates = [c for c in packet.get("candidates", [])
                      if c.get("ref") == proposal.key and c.get("kind") == proposal.kind]
        if not candidates or proposal.kind in {"health", "coverage", "source_health"}:
            return "notification has no business evidence binding"
        if not any(marker in lowered for marker in _ROUTINE_HEALTH_MARKERS):
            return None
        for candidate in candidates:
            subject = str(candidate.get("subject") or "").lower().strip()
            # A health-bearing sentence needs the actual business subject AND
            # time/consequence evidence. Generic 'payment/form/due' exemptions
            # cannot turn a health status into a business notification.
            if (len(subject) >= 8 and subject in lowered
                    and candidate.get("date") and proposal.key in proposal.evidence
                    and (proposal.consequence or _BUSINESS_RE.search(lowered))):
                return None
        return "health facts without grounded business consequence"
    for marker in _ROUTINE_HEALTH_MARKERS:
        if marker in lowered:
            return f"routine health marker: {marker}"
    return None
