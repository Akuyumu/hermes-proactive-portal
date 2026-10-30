"""Per-public-source health truth for status/doctor.

The historical capability report used the single newest ``last_success_at``
across every public feed. One healthy source therefore hid every failing one,
and one very old failure made an otherwise healthy public category look down.

This module keeps the original status vocabulary (``verified-fresh``,
``configured-stale``, ``configured-unverified`` ...) but evaluates each enabled
ordinary source and each adaptive coverage row independently:

* a source with a recent success is healthy;
* a source still inside its own cadence with valid cached evidence is healthy
  (``verified-cached``) -- it is not due, not failed;
* an explicit rate-limit/transient/deferred observation is neutral, never a
  successful read and never a failure of the whole category, and it may only be
  counted as cached when valid prior cache evidence actually exists;
* an explicit expiry/stale/terminal error overrides an old ``cache_at``;
* an LTA transport-gate skip or an explicit unsupported route is reported as an
  unresolved limitation, not as a source failure;
* the aggregate is ``partial`` when some sources are healthy and others are
  failing/unknown/unsupported, and ``unavailable`` only when every configured
  source is unreadable.

Nothing here performs a read, calls a model or changes a source.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from . import publicfeeds as _pf

STATUS_FRESH = "verified-fresh"
STATUS_CACHED = "verified-cached"
STATUS_PARTIAL = "configured-partial"
STATUS_FAILING = "configured-failing"
STATUS_STALE = "configured-stale"
STATUS_UNVERIFIED = "configured-unverified"
STATUS_UNSUPPORTED = "configured-unsupported"
STATUS_UNAVAILABLE = "unavailable"
STATUS_DISABLED = "disabled"
STATUS_UNSCOPED = "enabled-unscoped"

_INTENTIONAL_MARKERS = (
    "transport gate", "intentional", "skipped", "not due", "not-due",
    "budget", "deferred", "defer", "cadence", "per-run", "no candidate",
    "cooldown", "source-health",
)
_TRANSIENT_MARKERS = (
    "timeout", "timed out", "429", "rate limit", "rate-limit", "503", "502",
    "504", "temporar", "retryable", "transient", "network error",
)
_TERMINAL_MARKERS = (
    "expired", "expiry", "stale", "invalid", "malformed", "parse error",
    "unauthor", "forbidden", "revoked", "credential", "not found", "404",
    "401", "403", "api error", "fetch failed", "unsupported",
)
_SUCCESS_STATUSES = {"success", "truncated", "ok"}
_UNSUPPORTED_STATUSES = {"unsupported"}
_HEALTHY_STATUSES = {STATUS_FRESH, STATUS_CACHED}


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse(value: Any, now: datetime) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed


def _safe_int(value: Any, default: int | None) -> int | None:
    if type(value) is bool:
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _age_ok(iso: Any, now: datetime, minutes: int) -> bool:
    parsed = _parse(iso, now)
    if parsed is None or parsed > now:
        return False
    limit = _safe_int(minutes, 1)
    return now - parsed <= timedelta(minutes=max(1, int(limit or 1)))


def _reason_kind(text: Any) -> str:
    lowered = str(text or "").lower()
    if not lowered:
        return ""
    if any(marker in lowered for marker in _TERMINAL_MARKERS):
        return "terminal"
    if any(marker in lowered for marker in _INTENTIONAL_MARKERS):
        return "intentional"
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return "transient"
    return ""


def _neutral_reason(text: Any) -> bool:
    return _reason_kind(text) in {"intentional", "transient"}


def _health_row(health: dict[str, Any], name: str) -> tuple[dict[str, Any], bool]:
    row = health.get(name)
    if row is None:
        return {}, False
    if not isinstance(row, dict):
        return {}, True
    malformed = False
    if not isinstance(row.get("last_observation_kind", ""), str):
        malformed = True
    if not isinstance(row.get("failure_class", ""), str):
        malformed = True
    if row.get("failure_streak") not in (None, ""):
        if _safe_int(row.get("failure_streak"), None) is None:
            malformed = True
    return row, malformed


def _inactive(rec: dict[str, Any]) -> bool:
    if rec.get("parked") or rec.get("disabled"):
        return True
    if rec.get("enabled") is False:
        return True
    maintenance = rec.get("maintenance")
    if isinstance(maintenance, dict):
        state = str(maintenance.get("state") or "").strip().lower()
        if state in {"parked", "disabled", "off", "stopped"}:
            return True
    return False


def _cadence(rec: dict[str, Any], default: int) -> tuple[int, bool]:
    raw = rec.get("cadence_minutes")
    fallback = _safe_int(default, 60)
    fallback = max(1, int(fallback or 60))
    if raw in (None, ""):
        return fallback, True
    parsed = _safe_int(raw, None)
    if parsed is None or parsed <= 0:
        return fallback, False
    return parsed, True


def _cache_state(rec: dict[str, Any], now: datetime, cadence: int) -> tuple[bool, bool, str]:
    cache_at = str(rec.get("cache_at") or "").strip()
    if not cache_at:
        return False, False, ""
    parsed = _parse(cache_at, now)
    if parsed is None:
        return False, True, "cache timestamp malformed"
    if parsed > now:
        return False, True, "cache timestamp is in the future"
    if now - parsed > timedelta(minutes=max(1, int(cadence))):
        return False, False, "cache older than source cadence"
    items = rec.get("cache_items")
    if items is None:
        items = []
    if not isinstance(items, list):
        return False, True, "cached items malformed"
    try:
        usable, unsafe = _pf._usable_cache_items(items, now)
    except Exception:  # noqa: BLE001 - malformed cache never becomes healthy
        return False, True, "cached items malformed"
    if unsafe:
        return False, False, unsafe
    if len(usable) != len(items):
        return False, True, "cached items malformed"
    return True, False, ""


def ordinary_source_rows(
    registry: dict[str, Any],
    health: dict[str, Any],
    *,
    now: datetime,
    stale_minutes: int,
    default_cadence: int = 60,
) -> list[dict[str, Any]]:
    """One truthful row per enabled ordinary registry source."""
    rows: list[dict[str, Any]] = []
    sources = registry.get("sources") if isinstance(registry, dict) else {}
    if not isinstance(sources, dict):
        return rows
    for sid, rec in sorted(sources.items()):
        if not isinstance(rec, dict) or _inactive(rec):
            continue
        name = f"feed:{rec.get('id') or sid}"
        record, health_malformed = _health_row(health, name)
        cadence, cadence_ok = _cadence(rec, default_cadence)
        cache_valid, cache_malformed, cache_reason = _cache_state(rec, now, cadence)
        kind = str(rec.get("kind") or "")
        failure_class = str(record.get("failure_class") or "")
        observation = str(record.get("last_observation_kind") or "")
        stale_reason = str(rec.get("stale_reason") or "")
        last_success = str(record.get("last_success_at") or "")
        last_fetch = str(rec.get("last_fetch_at") or record.get("last_attempt_at") or "")
        reason_kind = _reason_kind(stale_reason)
        status = STATUS_UNVERIFIED
        fresh = False
        reason = ""

        if not cadence_ok or health_malformed or cache_malformed:
            status, fresh = STATUS_UNVERIFIED, False
            reason = cache_reason or "malformed source/cache/health record"
        else:
            lowered_reason = stale_reason.lower()
            hard_terminal = any(marker in lowered_reason for marker in (
                "expired", "expiry", "stale", "invalid", "malformed", "unauthor",
                "forbidden", "revoked", "credential", "unsupported",
            ))
            if failure_class in {"retryable", "rate_limited"}:
                terminal = hard_terminal
                transient = not terminal
            elif failure_class:
                terminal, transient = True, False
            else:
                terminal = reason_kind == "terminal"
                transient = reason_kind == "transient"
            if observation == "failure" and not terminal and not transient \
                    and reason_kind not in {"intentional"}:
                terminal = True
            intentional = reason_kind == "intentional" or observation == "non_evidential"
            if terminal:
                if "expired" in stale_reason.lower() or "stale" in stale_reason.lower():
                    status = STATUS_STALE
                else:
                    status = STATUS_FAILING
                fresh = False
                reason = stale_reason or "explicit failure observation"
            elif intentional:
                if cache_valid:
                    status, fresh = STATUS_CACHED, True
                    reason = stale_reason or "neutral observation; cached evidence valid"
                else:
                    status, fresh = STATUS_UNVERIFIED, False
                    reason = stale_reason or "neutral/deferred observation without valid cache"
            elif transient:
                if cache_valid:
                    status, fresh = STATUS_CACHED, True
                    reason = stale_reason or failure_class
                else:
                    status, fresh = STATUS_UNVERIFIED, False
                    reason = stale_reason or failure_class or "transient failure without valid cache"
            elif observation == "success":
                candidate = last_success or str(rec.get("cache_at") or "")
                if _age_ok(candidate, now, cadence):
                    status, fresh = STATUS_FRESH, True
                    last_success = candidate
                elif cache_valid:
                    status, fresh = STATUS_CACHED, True
                    reason = "success older than source cadence; cached evidence valid"
                else:
                    status, fresh = STATUS_STALE, False
                    reason = "last success older than source cadence"
            elif cache_valid:
                status, fresh = STATUS_CACHED, True
                reason = cache_reason
            elif cache_reason and (
                    "expired" in cache_reason.lower() or "stale" in cache_reason.lower()):
                status, fresh = STATUS_STALE, False
                reason = cache_reason
            elif last_fetch and not _age_ok(last_fetch, now, cadence):
                status, fresh = STATUS_STALE, False
                reason = "cadence elapsed without a successful read"
            else:
                status, fresh = STATUS_UNVERIFIED, False

        if status == STATUS_CACHED:
            last_success = str(rec.get("cache_at") or last_success)
        rows.append({
            "id": str(rec.get("id") or sid),
            "kind": kind,
            "interest": str(rec.get("interest") or ""),
            "status": status,
            "fresh": fresh,
            "healthy": status in _HEALTHY_STATUSES,
            "last_success_at": last_success,
            "last_attempt_at": last_fetch,
            "reason": reason,
            "cadence_minutes": cadence,
            "source": "ordinary",
        })
    return rows


def adaptive_source_rows(
    coverage: dict[str, Any] | None,
    *,
    now: datetime,
    stale_minutes: int,
) -> list[dict[str, Any]]:
    """One truthful row per adaptive coverage observation, preserving reasons.

    Fields mirror the real ``adaptive._record_coverage`` rows: ``status``,
    ``checked_at``, ``count``, ``error`` (plus an optional ``reason``); no
    synthetic ``last_success_at`` or ideal-fixture field is assumed.
    """
    rows: list[dict[str, Any]] = []
    watches = (coverage or {}).get("watches") if isinstance(coverage, dict) else {}
    if not isinstance(watches, dict):
        return rows
    for watch_id, entry in sorted(watches.items()):
        if not isinstance(entry, dict):
            continue
        sources = entry.get("sources")
        if not isinstance(sources, list):
            continue
        for index, item in enumerate(sources):
            if not isinstance(item, dict):
                continue
            raw_status = str(item.get("status") or "")
            error = str(item.get("error") or item.get("reason") or "")
            checked = str(item.get("checked_at") or entry.get("last_attempt_at") or "")
            reason_kind = _reason_kind(error)
            status = STATUS_UNVERIFIED
            fresh = False
            last_success = ""
            if raw_status in _SUCCESS_STATUSES and reason_kind != "terminal" and _age_ok(
                    checked, now, stale_minutes):
                if raw_status == "truncated" or error:
                    status = STATUS_CACHED
                else:
                    status, fresh = STATUS_FRESH, True
                last_success = checked
            elif raw_status in _SUCCESS_STATUSES and reason_kind == "terminal":
                status = STATUS_STALE if (
                    "expired" in error.lower() or "stale" in error.lower()
                ) else STATUS_FAILING
            elif raw_status in _UNSUPPORTED_STATUSES:
                status = STATUS_UNSUPPORTED
            elif reason_kind == "terminal":
                status = STATUS_STALE if (
                    "expired" in error.lower() or "stale" in error.lower()
                ) else STATUS_FAILING
            elif reason_kind in {"intentional", "transient"}:
                status = STATUS_UNVERIFIED
            elif raw_status in {"failed", "failure", "error", "stale"}:
                status = STATUS_FAILING
            rows.append({
                "id": f"{watch_id}:{index}",
                "watch_id": str(watch_id),
                "kind": str(item.get("kind") or ""),
                "interest": str(item.get("interest") or ""),
                "status": status,
                "fresh": fresh,
                "healthy": status in _HEALTHY_STATUSES,
                "last_success_at": last_success,
                "last_attempt_at": checked,
                "reason": error,
                "source": "adaptive",
            })
    return rows


def aggregate(rows: list[dict[str, Any]]) -> str:
    """Classify aggregate public coverage without letting one row dominate."""
    if not rows:
        return "unknown"
    healthy = [r for r in rows if r.get("healthy")]
    failing = [r for r in rows if r.get("status") == STATUS_FAILING]
    unsupported = [r for r in rows if r.get("status") == STATUS_UNSUPPORTED]
    if healthy and any(not r.get("healthy") for r in rows):
        return "partial"
    if healthy:
        return "complete"
    if failing:
        return "unavailable"
    if unsupported:
        return "unsupported"
    return "unknown"


def public_truth(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    registry: dict[str, Any] | None = None,
    adaptive: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return the truthful ``public`` capability entry.

    With no registry/coverage supplied this preserves the legacy health-only
    behaviour for callers/tests that only have configuration and notify state.
    """
    now = _now(now)
    health_root = state.get("source_health") or {}
    health = health_root.get("sources") if isinstance(health_root, dict) else {}
    if not isinstance(health, dict):
        health = {}
    stale = _safe_int(
        health_root.get("stale_after_minutes") if isinstance(health_root, dict) else None,
        45,
    )
    stale = max(1, int(stale or 45))
    sources_cfg = config.get("sources") or {}
    enabled = bool(sources_cfg.get("public_feeds")) if isinstance(sources_cfg, dict) else False
    scope = list(config.get("public_sources") or config.get("public_interests") or [])
    rows: list[dict[str, Any]] = []

    legacy = registry is None and adaptive is None
    if legacy:
        records = [row for key, row in health.items()
                   if isinstance(row, dict)
                   and (key == "public" or str(key).startswith(("public:", "feed:")))]
        successes = sorted(str(row.get("last_success_at") or "") for row in records
                           if row.get("last_success_at"))
        last = successes[-1] if successes else ""
        fresh = False
        freshness = "not-configured"
        if enabled and scope and last:
            fresh = _age_ok(last, now, stale)
            freshness = "fresh" if fresh else "stale"
        verified = bool(last)
        if not enabled:
            status, remedy = STATUS_DISABLED, "Enable this source in the same native/CLI configuration when wanted."
        elif not scope:
            status, remedy = STATUS_UNSCOPED, "Add an explicit account/source scope; installed binaries are not coverage."
        elif not verified:
            status, remedy = STATUS_UNVERIFIED, "No successful read recorded yet; run a check after authorising access."
        elif not fresh:
            status, remedy = STATUS_STALE, "Last successful read is stale; review source health or authorisation."
        else:
            status, remedy = STATUS_FRESH, ""
        return {
            "available": True, "configured": bool(enabled and scope),
            "authorized": bool(scope), "verified": verified, "fresh": bool(fresh and enabled),
            "freshness": freshness, "last_success_at": last, "status": status,
            "remedy": remedy, "coverage": ("fresh" if fresh else "stale") if verified else "none",
            "sources": [], "source_count": 0, "healthy_count": 0,
            "degraded_count": 0, "unsupported_count": 0,
        }

    if registry is not None:
        default_cadence = _safe_int(config.get("feed_cadence_minutes", 60), 60) or 60
        rows.extend(ordinary_source_rows(
            registry, health, now=now, stale_minutes=stale,
            default_cadence=int(default_cadence)))
    rows.extend(adaptive_source_rows(adaptive, now=now, stale_minutes=stale))
    configured = bool((enabled and scope) or rows)
    coverage = aggregate(rows)
    healthy = [r for r in rows if r.get("healthy")]
    failing = [r for r in rows if r.get("status") == STATUS_FAILING]
    unsupported = [r for r in rows if r.get("status") == STATUS_UNSUPPORTED]
    last_success = max(
        (str(r.get("last_success_at") or "") for r in rows if r.get("last_success_at")),
        default="",
    )
    verified = bool(healthy)
    fresh = any(r.get("status") == STATUS_FRESH for r in rows)
    if not configured:
        status = STATUS_DISABLED
        remedy = "Enable this source in the same native/CLI configuration when wanted."
    elif not scope and not rows:
        status = STATUS_UNSCOPED
        remedy = "Add an explicit account/source scope; installed binaries are not coverage."
    elif coverage == "partial":
        status = STATUS_PARTIAL
        remedy = "Some public sources are failing or unsupported; healthy sources continue."
    elif coverage == "unavailable":
        status = STATUS_FAILING
        remedy = "Every configured public source is currently unreadable; review source health."
    elif coverage == "unsupported":
        status = STATUS_UNSUPPORTED
        remedy = "The configured public route is unsupported; no automatic expansion is allowed."
    elif coverage == "unknown":
        status = STATUS_UNVERIFIED
        remedy = "No successful public read recorded yet; run a check after authorising access."
    elif coverage == "none":
        status = STATUS_UNVERIFIED
        remedy = "No public source record yet; run a check after authorising access."
    elif fresh:
        status, remedy = STATUS_FRESH, ""
    else:
        status, remedy = STATUS_CACHED, ""
    return {
        "available": True, "configured": configured, "authorized": bool(scope or rows),
        "verified": verified, "fresh": fresh,
        "freshness": ("fresh" if fresh else "stale" if failing else "cached" if healthy else "not-configured"),
        "last_success_at": last_success, "status": status, "remedy": remedy,
        "coverage": coverage, "sources": rows, "source_count": len(rows),
        "healthy_count": len(healthy), "degraded_count": len(failing),
        "unsupported_count": len(unsupported),
    }
