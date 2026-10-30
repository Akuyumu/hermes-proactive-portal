"""Feedback-responsive interest acquisition and relevance.

This reuses the existing preference machinery (:mod:`proactive_pilot.feedback`
and :func:`proactive_pilot.policy.preference_blocks`); it does not add a new
agent loop, model call or permission inference.

* A user correction (``watch-change`` / ``watch-feedback``) already revises the
  watch topic/criteria and the next plan, so acquisition follows the correction.
* An explicit suppression or a scoped temporary snooze on the watch/interest
  blocks both the next acquisition and matching evidence this cycle.
* Snoozes expire by clock comparison: the same watch resumes automatically.
* Conditional explicit rules are NOT blanket suppressions (the evaluator keeps
  deciding fit), and inferred preferences remain advisory.
* "Thanks"/silence changes nothing: no watch or goal is revoked here.
* An explicit watch is never retired by this module; only an explicit user
  action retires it. Inferred goals may be blocked/deprioritised, never promoted
  to an explicit watch.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _item_for_watch(watch: dict[str, Any]) -> dict[str, Any]:
    topic = str(watch.get("public_topic") or watch.get("interest") or "")
    include = " ".join(str(v) for v in (watch.get("include") or []))
    return {
        "kind": "feed",
        "subject": topic,
        "message": (topic + " " + include).strip(),
        "source": str(watch.get("id") or ""),
    }


def watch_allowed(watch: dict[str, Any], policy: dict[str, Any], now: datetime | None = None) -> bool:
    """True when no explicit suppression or active scoped snooze covers a watch.

    A present-but-corrupt preference artifact fails CLOSED. Only a genuinely
    absent preference store (standalone helper/tests) has nothing to enforce.
    """
    if not isinstance(policy, dict):
        return False
    if "_preferences" in policy and not isinstance(policy.get("_preferences"), dict):
        return False
    prefs = policy.get("_preferences")
    if not isinstance(prefs, dict):
        return True
    from .policy import active_snooze, preference_blocks

    now = _now(now)
    if active_snooze(prefs, now):
        return False
    return not preference_blocks(_item_for_watch(watch), prefs, now)


def item_allowed(item: dict[str, Any], policy: dict[str, Any], now: datetime | None = None) -> bool:
    """True when matching adaptive evidence is not explicitly suppressed/snoozed."""
    if not isinstance(policy, dict) or not isinstance(item, dict):
        return False
    if "_preferences" in policy and not isinstance(policy.get("_preferences"), dict):
        return False
    prefs = policy.get("_preferences")
    if not isinstance(prefs, dict):
        return True
    from .policy import active_snooze, preference_blocks

    now = _now(now)
    if active_snooze(prefs, now):
        return False
    candidate = {
        "kind": "feed",
        "subject": str(item.get("title") or item.get("subject") or item.get("interest") or ""),
        "message": str(item.get("summary") or ""),
        "source": str(item.get("source") or ""),
    }
    return not preference_blocks(candidate, prefs, now)


def filter_watches(
    watches: list[dict[str, Any]], policy: dict[str, Any], now: datetime | None = None
) -> list[dict[str, Any]]:
    """Keep only watches whose explicit/temporary feedback currently allows them."""
    return [w for w in watches if watch_allowed(w, policy, now)]


def filter_interest_targets(
    interests: list[str], policy: dict[str, Any], now: datetime | None = None
) -> list[str]:
    """Apply the same suppression/snooze boundary to approved interests."""
    out: list[str] = []
    for interest in interests:
        watch = {"id": "interest", "public_topic": interest, "include": []}
        if watch_allowed(watch, policy, now):
            out.append(interest)
    return out


def filter_results(results: list[Any], policy: dict[str, Any], now: datetime | None = None) -> int:
    """Drop matching evidence in-place; return the number of removed items."""
    removed = 0
    stamp = _now(now)
    for result in results:
        items = getattr(result, "items", None)
        if not items:
            continue
        kept = [item for item in items
                if not isinstance(item, dict) or item_allowed(item, policy, stamp)]
        removed += len(items) - len(kept)
        if len(kept) != len(items):
            result.items = kept
            try:
                result.count = len(kept)
            except Exception:  # noqa: BLE001 - read-only best effort
                pass
    return removed


def acknowledgement_noop() -> dict[str, Any]:
    """The explicit no-op for "thanks"/silence: no preference or watch change."""
    return {"status": "no-op", "reason": "acknowledgement only; no interest inference"}
