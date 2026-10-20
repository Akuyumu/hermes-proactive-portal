"""Bounded emitted-alert references for mobile follow-up context.

The plugin has no privileged access to platform quote metadata or session rows.
Instead, at the moment an alert is emitted it records a small opaque reference in
its own profile-scoped plugin state: an opaque id, the candidate/source version,
the destination and the emission time. The safe ``pre_llm_call`` hook can then
offer the foreground agent *actual* bounded references instead of generic
keyword instructions. This is an emission record, not a delivery receipt.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

STATE_KEY = "alert_references"
MAX_REFS = 5
MAX_CANDIDATES = 3


def _short(value: Any, limit: int = 160) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    return text[:limit]


def make_ref(signature: Any, kind: Any = "", at: Any = "") -> str:
    digest = hashlib.sha256(
        f"{_short(signature)}{_short(kind)}{_short(at)}".encode("utf-8")).hexdigest()
    return f"sec-{digest[:12]}"


def remember_alert(state_facade: Any, *, signature: Any, kind: Any = "",
                   candidate_refs: Iterable[Mapping[str, Any]] = (),
                   destination: Any = "", emitted_at: Any = "",
                   source_version: Any = "") -> dict[str, Any] | None:
    """Append one bounded emission reference; never raises into delivery."""
    if state_facade is None:
        return None
    ref = make_ref(signature, kind, emitted_at)
    candidates = []
    for item in list(candidate_refs)[:MAX_CANDIDATES]:
        if not isinstance(item, Mapping):
            continue
        candidate = {
            "ref": _short(item.get("ref"), 128),
            "version": _short(item.get("version"), 64),
            "source": _short(item.get("source") or item.get("kind"), 64),
        }
        candidates.append({key: value for key, value in candidate.items() if value})
    record = {
        "ref": ref,
        "signature": _short(signature, 128),
        "kind": _short(kind, 64),
        "candidates": candidates,
        "destination": _short(destination, 128),
        "emitted_at": _short(emitted_at, 64),
        "source_version": _short(source_version, 64),
        # Emitted by this plugin; the native scheduler owns delivery confirmation.
        "delivery": "emitted-unconfirmed",
    }
    try:
        previous = state_facade.get(STATE_KEY)
        rows = list(previous) if isinstance(previous, list) else []
        rows = [row for row in rows if isinstance(row, Mapping) and row.get("ref") != ref]
        rows.append(record)
        state_facade.set(STATE_KEY, rows[-MAX_REFS:])
    except Exception:  # noqa: BLE001 - context bookkeeping never breaks emission
        return record
    return record


def recent_alert_refs(state_facade: Any, *, limit: int = MAX_REFS,
                      ref: str = "") -> list[dict[str, Any]]:
    if state_facade is None:
        return []
    try:
        rows = state_facade.get(STATE_KEY)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(rows, list):
        return []
    clean = [dict(row) for row in rows if isinstance(row, Mapping)]
    if ref:
        clean = [row for row in clean if str(row.get("ref") or "") == ref]
    return clean[-max(0, min(int(limit), MAX_REFS)):]


def alert_context(state_facade: Any, *, ref: str = "", limit: int = 2) -> list[str]:
    """Return short, bounded lines describing emitted alerts (no raw evidence)."""
    lines: list[str] = []
    for row in recent_alert_refs(state_facade, limit=limit, ref=ref):
        candidates = row.get("candidates") if isinstance(row.get("candidates"), list) else []
        candidate_text = ",".join(
            f"{item.get('ref')}@{item.get('version') or '?'}" for item in candidates
            if isinstance(item, Mapping) and item.get("ref"))
        line = (f"{row.get('ref')} kind={row.get('kind') or '?'}"
                f" candidate={candidate_text or '?'}"
                f" destination={row.get('destination') or '?'}"
                f" emitted_at={row.get('emitted_at') or '?'}"
                f" delivery={row.get('delivery') or 'emitted-unconfirmed'}")
        lines.append(line[:400])
    return lines


__all__ = ["STATE_KEY", "make_ref", "remember_alert", "recent_alert_refs", "alert_context"]
