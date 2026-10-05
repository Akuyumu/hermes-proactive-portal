"""Deterministic candidate assembly + evidence packet for the evaluator.

This layer turns raw bounded source items into a compact, redacted packet and a
list of candidate keys. It does NOT rank importance and does NOT decide to send.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any

from .collectors import SourceResult
from .policy import is_noise

SCHEMA_VERSION = 1
MAX_PREVIEW_CHARS = 240
MAX_CONTEXT_ITEMS = 5

# Phases that represent a genuinely time-critical matter. Lower rank is more
# critical; an absent phase is not critical.
_CRITICAL_PHASE_RANK = {
    "overdue": 0,
    "within30min": 0,
    "due_today": 1,
    "within90min": 1,
    "approaching": 2,
}


def _critical_rank(candidate: dict[str, Any]) -> int | None:
    return _CRITICAL_PHASE_RANK.get(str(candidate.get("phase", "")))


def _kind_capped(candidates: list[dict[str, Any]], limit_per_kind: int) -> list[dict[str, Any]]:
    """Round-robin sample each kind down to ``limit_per_kind``.

    Each kind keeps its relative order (preserving original priority) but a
    single huge bucket cannot expand the packet.
    """
    if limit_per_kind <= 0:
        return []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for cand in candidates:
        buckets.setdefault(str(cand.get("kind", "")), []).append(cand)
    out: list[dict[str, Any]] = []
    kinds = sorted(buckets)
    taken: dict[str, int] = {kind: 0 for kind in kinds}
    while any(buckets.values()):
        progressed = False
        for kind in kinds:
            bucket = buckets[kind]
            if not bucket:
                continue
            if taken[kind] >= limit_per_kind:
                # Drop capped surplus so a non-empty bucket cannot spin forever.
                bucket.clear()
                continue
            out.append(bucket.pop(0))
            taken[kind] += 1
            progressed = True
        if not progressed:
            break
    return out


def _fair_sample(
    candidates: list[dict[str, Any]], limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fair round-robin sample across (source, kind) buckets within ``limit``.

    A single source/kind that produces 300 overdue items cannot take every seat.
    Each bucket's original order is preserved. Returns ``(selected, omitted)``.
    """
    if limit <= 0:
        return [], list(candidates)
    buckets: dict[tuple[str, str], list[int]] = {}
    for i, cand in enumerate(candidates):
        buckets.setdefault((str(cand.get("source", "")), str(cand.get("kind", ""))), []).append(i)
    selected: list[int] = []
    keys = sorted(buckets)
    while len(selected) < limit and any(buckets.values()):
        progressed = False
        for key in keys:
            bucket = buckets[key]
            if not bucket:
                continue
            selected.append(bucket.pop(0))
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break
    chosen = set(selected)
    ordered = [i for i in range(len(candidates)) if i in chosen]
    omitted = [candidates[i] for i in range(len(candidates)) if i not in chosen]
    return [candidates[i] for i in ordered], omitted


def _cap_candidates(
    candidates: list[dict[str, Any]], limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bound the packet without letting one kind starve another.

    Truly time-critical candidates are seated first (by criticality, then
    original order), then remaining slots are handed out round-robin across
    candidate kinds so a large task inventory can never crowd out mail or
    calendar evidence. Returns ``(selected, omitted)``.
    """
    if len(candidates) <= limit:
        return list(candidates), []
    immediate = [
        i for i, c in enumerate(candidates)
        if _critical_rank(c) == 0 and str(c.get("kind")) != "tasks"
    ][:limit]
    selected: set[int] = set(immediate)
    remaining = [c for i, c in enumerate(candidates) if i not in selected]
    picked, omitted = _fair_sample(remaining, limit - len(selected))
    return [candidates[i] for i in sorted(selected)] + picked, omitted


def _event_version(item: dict[str, Any]) -> str:
    """Meaningful-content version: identity of the source object's content.

    Includes the event/task status (so a cancellation or completion is a real
    change) and the content-bearing fields, but never volatile retrieval
    metadata such as ``checked_at``.
    """
    fields = (
        "title", "summary", "start", "end", "due_date", "status",
        "updated", "sender", "subject", "date", "category", "confidence",
    )
    raw = "|".join(str(item.get(field, "")) for field in fields)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _time_phase(kind: str, raw: str, *, now: datetime, all_day: bool = False) -> str:
    """Meaningful time phase used for wake/dedupe, not for importance.

    Events: passed / within30min / within90min / far (all-day -> allday).
    Tasks: overdue / due_today / approaching / far.
    Obligations: undated (no urgency) / overdue / due_today / approaching / far.
    Public feeds: fresh / recent / old (informational only; never urgent).
    """
    text = str(raw or "").strip()
    if not text:
        return "undated" if kind == "obligation" else "far"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return "undated" if kind == "obligation" else "far"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    if kind == "feed":
        age = (now - parsed).total_seconds()
        if age <= 24 * 3600:
            return "fresh"
        if age <= 7 * 24 * 3600:
            return "recent"
        return "old"
    if kind in {"tasks", "obligation"}:
        due = parsed.date()
        today = now.date()
        if due < today:
            return "overdue"
        if due == today:
            return "due_today"
        if due <= today + timedelta(days=1):
            return "approaching"
        return "far"
    if all_day or (len(text) == 10):
        return "allday"
    seconds = (parsed - now).total_seconds()
    if seconds < 0:
        return "passed"
    if seconds <= 30 * 60:
        return "within30min"
    if seconds <= 90 * 60:
        return "within90min"
    return "far"


def build_packet(
    results: list[SourceResult],
    *,
    clock: Any,
    policy: dict[str, Any],
    baseline: dict[str, Any],
    context: list[str] | None = None,
    private_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the evaluator evidence packet.

    Calendar/tasks/mail items are candidates regardless of prior inventory; the
    evaluator sees a bounded set of material and decides relevance. Generic
    career/job-list noise is flagged (not removed) so the evaluator can default
    to suppressing it unless it is exceptionally fitting.
    """
    candidates: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {}
    now = clock.now()

    for result in results:
        cached = bool(getattr(result, "cached", False))
        cache_at = str(getattr(result, "cache_at", "") or "")
        coverage[result.name] = {
            # A truncated read is explicitly declared partial, never complete.
            "status": "partial" if result.truncated else result.status,
            "count": result.count,
            "truncated": bool(result.truncated),
            "error": result.error,
            # When serving cached evidence, expose the true observation time so
            # downstream freshness checks cannot mistake stale cache for fresh.
            "checked_at": cache_at if cached and cache_at else result.checked_at,
            "cached": cached,
            "cache_age_minutes": int(getattr(result, "cache_age_minutes", 0) or 0),
            "failure": str(getattr(result, "failure", "") or ""),
            # Safe, non-private scan progress for large historical sources: how
            # many metadata rows were examined, pages read, and the resume
            # cursor. Proves coverage without exposing raw private fields.
            "scanned": int(getattr(result, "scanned", 0) or 0),
            "ids_listed": int(getattr(result, "ids_listed", 0) or 0),
            "pages_read": int(getattr(result, "pages_read", 0) or 0),
            "next_page": int(getattr(result, "next_page", 0) or 0),
            "window_complete": bool(getattr(result, "window_complete", False)),
            # A resumable provider cursor exists (boolean only: the opaque token
            # and the queued ids themselves are never exposed in the packet).
            "has_more": bool(getattr(result, "next_token", "")
                             or getattr(result, "next_page", 0)
                             or getattr(result, "pending_ids", [])),
        }
        if not result.healthy:
            continue
        kind = result.name.split(":", 1)[0]
        for item in result.items:
            when = (item.get("valid_from") or item.get("date")
                    or item.get("start") or item.get("due_date", ""))
            entry = {
                "kind": kind,
                "source": result.name,
                "ref": item.get("ref", ""),
                "version": _event_version(item),
                "subject": item.get("subject") or item.get("title") or "",
                "sender": item.get("sender", ""),
                "date": when,
                "all_day": item.get("all_day", False),
                "has_attachment": item.get("has_attachment", False),
                "status": item.get("status", ""),
                "phase": _time_phase(kind, str(when), now=now, all_day=bool(item.get("all_day", False))),
                "sensitive": bool(item.get("sensitive", False)),
                "preview": str(item.get("preview", ""))[:MAX_PREVIEW_CHARS],
                # Explicit evidence labels: confidence, observed age and cache
                # state are surfaced so a historical bootstrap cannot present an
                # old item as fresh, and the evaluator never invents urgency.
                "confidence": str(item.get("confidence", "")),
                "age_days": int(item.get("age_days", 0) or 0),
                "historical": bool(item.get("historical", False)),
                "cached": cached or bool(item.get("cached", False)),
                "noise_like": is_noise({**item, "source": result.name}, policy),
                # Honest evidence provenance: an inferred autonomous discovery is
                # labelled inferred and never presented to the evaluator as a
                # user-requested watch. (Absent on non-feed items.)
                "origin": str(item.get("origin") or ""),
                "provenance": str(item.get("provenance") or "")[:300],
                "interest": str(item.get("interest") or "")[:200],
            }
            # Specialized public-source context is kept only for the reviewed
            # forecast/planned-notice records; generic feeds retain the compact
            # baseline packet shape.
            if kind == "feed" and item.get("category") in {
                "weather_forecast", "planned_transport", "opportunity"
            }:
                entry.update({
                    "category": str(item.get("category") or ""),
                    "publisher": str(item.get("publisher") or ""),
                    "summary": str(item.get("summary") or "")[:700],
                    "published": str(item.get("date") or "")[:64],
                    "valid_from": str(item.get("valid_from") or "")[:64],
                    "valid_until": str(item.get("valid_until") or "")[:64],
                    "affected_lines": list(item.get("affected_lines") or [])[:8],
                    "forecast_only": bool(item.get("forecast_only", False)),
                    "planned_notice": bool(item.get("planned_notice", False)),
                    "opportunity_only": bool(item.get("opportunity_only", False)),
                    "deadline": str(item.get("deadline") or item.get("valid_until") or "")[:64],
                    # Deadline precision truth: an explicit local closing time vs
                    # a date-only deadline whose closing time is unknown.
                    "deadline_precision": str(item.get("deadline_precision") or "")[:16],
                    "closing_time_known": bool(item.get("closing_time_known", False)),
                    # Source-declared audience only; never a personal qualification.
                    "audience": str(item.get("audience") or "")[:300],
                    "audience_declared": bool(item.get("audience_declared", False)),
                    "personal_qualification": False,
                })
            candidates.append(entry)

    packet_limit = max(1, min(int(policy.get("max_packet_candidates", 200)), 400))
    candidates = _kind_capped(candidates, int(policy.get("max_candidates_per_kind", 60)))
    selected, omitted = _cap_candidates(candidates, packet_limit)
    for dropped in omitted:
        cov = coverage.get(dropped.get("source"))
        if isinstance(cov, dict):
            cov["status"] = "partial"
            cov["truncated"] = True

    packet: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "quiet_hours": _quiet_label(policy),
        "daily_normal_cap": int(policy.get("daily_normal_cap", 3)),
        "coverage": coverage,
        "known_task_refs": list(baseline.get("task_refs", []))[:200],
        "candidates": selected,
    }
    if context:
        packet["user_context"] = [str(line)[:MAX_PREVIEW_CHARS] for line in list(context)[:MAX_CONTEXT_ITEMS]]
    if private_context:
        # Derived, coordinate-free phone context (see proactive_pilot.location).
        # Deliberately excluded from packet_digest: supplementary context must not
        # churn the digest or fabricate urgency.
        packet["private_context"] = dict(private_context)
    return packet


def _quiet_label(policy: dict[str, Any]) -> str:
    return f"{int(policy.get('quiet_start_hour',0)):02d}:00-{int(policy.get('quiet_end_hour',8)):02d}:00"


def packet_digest(packet: dict[str, Any]) -> str:
    """Stable material digest.

    Excludes ``generated_at``, every ``checked_at`` and every retrieval-only
    field (``cached``/``cache_age``/``failure``) so an idle run over unchanged
    evidence does not look like new material -- including a 304 that refreshes
    the observation time while the evidence itself is byte-identical. Source
    health/status/count and each candidate's ref/version/phase are still folded
    in, so a real change (truncation, failure-to-cover, moved phase, new version)
    is never hidden.
    """
    coverage = {}
    for name, cov in (packet.get("coverage") or {}).items():
        if not isinstance(cov, dict):
            continue
        coverage[name] = {
            "status": cov.get("status"),
            "truncated": bool(cov.get("truncated")),
            "count": cov.get("count"),
            "error": cov.get("error"),
        }
    material = {
        "schema": packet.get("schema"),
        "quiet_hours": packet.get("quiet_hours"),
        "daily_normal_cap": packet.get("daily_normal_cap"),
        "coverage": coverage,
        "known_task_refs": list(packet.get("known_task_refs", []))[:200],
        "candidates": [
            {
                "kind": c.get("kind"),
                "source": c.get("source"),
                "ref": c.get("ref"),
                "version": c.get("version"),
                "phase": c.get("phase"),
                "status": c.get("status"),
                "sensitive": bool(c.get("sensitive")),
            }
            for c in packet.get("candidates", [])
        ],
    }
    # A MATERIAL change to the derived local phone context (bound event, status,
    # availability, approximate distance bucket) must affect the digest/wake, so a
    # new/changed location fact is not indefinitely ignored. Non-material fields
    # (attempt flags, timestamps) are excluded to avoid idle churn.
    private = packet.get("private_context")
    if isinstance(private, dict) and private:
        material["private_context"] = {
            "ref": str(private.get("ref", "")),
            "status": str(private.get("status", "")),
            "available": bool(private.get("available")),
            "fresh": bool(private.get("fresh")),
            "destination_resolved": bool(private.get("destination_resolved")),
            "distance_approx_m": private.get("distance_approx_m"),
        }
    body = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def is_unchanged_task_list(packet: dict[str, Any], state: dict[str, Any]) -> bool:
    """True only when the task inventory is provably unchanged AND not material.

    Never hides a changed due date, status, version or time phase: a task whose
    phase is time-critical, or whose baseline record differs in any meaningful
    field, always returns False (i.e. must wake). A task whose due date is at/near
    the packet generation date stays observable even when its id is unchanged:
    approaching a due date is relevant evidence on its own.
    """
    task_cands = [c for c in packet["candidates"] if c["kind"] == "tasks"]
    task_refs = [c["ref"] for c in task_cands]
    if not task_refs:
        return False

    baseline_map = state.get("baseline_tasks")
    if isinstance(baseline_map, dict) and baseline_map:
        for cand in task_cands:
            prior = baseline_map.get(cand["ref"])
            if not isinstance(prior, dict):
                return False
            for field in ("version", "status", "due_date", "phase"):
                if str(prior.get(field, "")) != str(cand.get(field, "")):
                    return False
        return True

    seen = set(state.get("baseline_task_refs", []))
    if not set(task_refs).issubset(seen):
        return False
    for cand in task_cands:
        if _critical_rank(cand) is not None:
            return False
    try:
        today = date.fromisoformat(str(packet.get("generated_at", ""))[:10])
    except ValueError:
        return True
    near = today + timedelta(days=1)
    for cand in task_cands:
        due = str(cand.get("due_date") or cand.get("date") or "")[:10]
        if not due:
            continue
        try:
            if date.fromisoformat(due) <= near:
                return False
        except ValueError:
            continue
    return True


def candidate_signature(item: dict[str, Any]) -> str:
    """Stable event identity: source object + version + meaningful time phase.

    Message wording is deliberately excluded so a paraphrase of the same
    source event/version is never a new notification. A changed version (e.g. a
    moved calendar start or a cancellation) or a new time phase is a genuinely
    distinct event.
    """
    raw = f"{item.get('kind','')}|{item.get('ref','')}|{item.get('version','')}|{item.get('phase','')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
