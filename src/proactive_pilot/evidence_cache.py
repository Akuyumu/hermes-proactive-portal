"""Bounded source-evidence cache for already-authorised preparation facts.

This is deliberately NOT a connector, fetcher, command runner or source
framework. It only records items that an existing enabled/consented collector
already returned during a normal run, and lets the private-preparation preflight
read a bounded, fresh subset for the responsibility's current refs/scopes.

Rules:
- nothing is recorded unless preparation + source consent are both enabled;
- a disabled source, missing account/calendar consent, revoked scope or stale
  item yields ZERO reads (never an implicit network/connector call);
- only bounded text/ref/provenance metadata is stored; no raw bodies, paths,
  credentials or private identifiers become public query material;
- reads are deterministic and offline.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .state import StateStore

STORE = "source_evidence.json"
SCHEMA = 1
LOCK = "source_evidence.lock"
MAX_ITEMS = 200
MAX_TEXT = 600
MAX_REF = 240
DEFAULT_FRESH_SECONDS = 24 * 60 * 60


def _scope_for_name(name: Any) -> str | None:
    text = str(name or "").lower()
    if text.startswith("mail"):
        return "mail"
    if text.startswith("calendar"):
        return "calendar"
    if text.startswith("tasks"):
        return "tasks"
    if text.startswith(("feed", "public", "github", "reddit", "web")):
        return "public"
    return None


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return text[:limit]


def _source_authorized(policy: dict[str, Any], scope: str) -> bool:
    if scope == "mail":
        return (bool(policy.get("mail_enabled", False))
                and bool(policy.get("mail_accounts") or policy.get("mail_account_aliases")))
    if scope == "calendar":
        return (bool(policy.get("calendar_enabled", False))
                and bool(policy.get("calendar_ids") or policy.get("calendar_accounts")))
    if scope == "tasks":
        return (bool(policy.get("tasks_enabled", False))
                and bool(policy.get("task_accounts") or policy.get("task_lists")
                         or policy.get("task_adapter")))
    if scope == "public":
        return bool(policy.get("feeds_enabled", True)
                    or policy.get("adaptive_mode") == "active"
                    or policy.get("adaptive_evidence_enabled"))
    return False


def scope_enabled(policy: dict[str, Any], scope: str) -> bool:
    """Public predicate: is this source scope currently enabled/consented?"""
    return _source_authorized(policy, scope)


def record_results(store: StateStore, results: Iterable[Any], *,
                   policy: dict[str, Any], now: datetime | None = None) -> int:
    """Record bounded items from an ordinary run; zero writes without consent."""
    if not (bool(policy.get("preparation_enabled"))
            and bool(policy.get("preparation_source_consent"))):
        return 0
    scopes = {str(s).lower() for s in (policy.get("preparation_scopes") or [])
              if isinstance(s, str)}
    if not scopes:
        return 0
    stamp = now or datetime.now(timezone.utc)
    rows: list[dict[str, str]] = []
    for result in results:
        scope = _scope_for_name(getattr(result, "name", ""))
        if scope is None or scope not in scopes:
            continue
        if str(getattr(result, "status", "")) != "ok":
            continue
        if not _source_authorized(policy, scope):
            continue
        checked = _clean(getattr(result, "checked_at", ""), 64) or stamp.isoformat()
        for item in list(getattr(result, "items", []) or [])[:40]:
            if not isinstance(item, dict):
                continue
            ref = _clean(item.get("ref") or item.get("id"), MAX_REF)
            text = _clean(item.get("subject") or item.get("title")
                          or item.get("summary") or item.get("snippet"), MAX_TEXT)
            version = _clean(item.get("version") or item.get("source_version"), MAX_REF)
            if not ref or not text:
                continue
            rows.append({
                "scope": scope,
                "ref": ref,
                "text": text,
                "source": _clean(getattr(result, "name", ""), 80),
                "source_version": version or checked,
                "observed_at": _clean(item.get("date") or checked, 64),
            })
    if not rows:
        return 0
    with store.lock(LOCK) as acquired:
        if not acquired:
            return 0
        data = store.load(STORE, {"schema": SCHEMA, "items": []}, strict=True)
        existing = list(data.get("items") or []) if isinstance(data, dict) else []
        merged: dict[str, dict[str, str]] = {}
        for row in existing + rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("ref") or "")
            if key:
                merged[key] = {k: str(row.get(k) or "") for k in
                               ("scope", "ref", "text", "source",
                                "source_version", "observed_at")}
        items = sorted(merged.values(),
                       key=lambda row: (row.get("observed_at", ""), row.get("ref", "")))[-MAX_ITEMS:]
        store.save(STORE, {"schema": SCHEMA, "items": items})
        return len(rows)


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def read_facts(store: StateStore, *, scopes: set[str], refs: set[str],
               source_version: str = "", now: datetime | None = None,
               max_age_seconds: int = DEFAULT_FRESH_SECONDS,
               limit: int = 24) -> list[dict[str, str]]:
    """Return fresh authorised facts for exactly these scopes/refs (offline)."""
    stamp = now or datetime.now(timezone.utc)
    cutoff = stamp - timedelta(seconds=max(60, int(max_age_seconds)))
    try:
        data = store.load(STORE, {"schema": SCHEMA, "items": []}, strict=True)
    except Exception:  # noqa: BLE001 - corrupt/absent cache is simply no evidence
        return []
    rows = data.get("items") if isinstance(data, dict) else []
    out: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        scope = str(row.get("scope") or "")
        if scope not in scopes:
            continue
        ref = str(row.get("ref") or "")
        if not ref:
            continue
        version = str(row.get("source_version") or "")
        if source_version and version and version != source_version:
            continue
        observed = _parse(row.get("observed_at"))
        if observed is None or observed < cutoff or observed > stamp + timedelta(minutes=5):
            continue
        if refs and ref not in refs:
            continue
        out.append({
            "ref": ref,
            "text": str(row.get("text") or "")[:MAX_TEXT],
            "provenance": str(row.get("source") or scope)[:80],
            "observed_at": observed.isoformat(),
            "trust": "untrusted_source_data",
        })
        if len(out) >= max(1, int(limit)):
            break
    return out


def digest(facts: list[dict[str, Any]]) -> str:
    body = "\n".join(sorted(str(f.get("ref") or "") + "|" + str(f.get("text") or "")
                            for f in facts))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
