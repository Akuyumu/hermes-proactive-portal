"""Broad public source discovery + strict vetted-source registration.

This is the bridge from a validated adaptive watch plan to *broad* public web
evidence -- deliberately NOT GitHub/Reddit only. Flow:

    watch plan (bounded queries)
        -> native search bridge (existing configured provider; fake in tests)
        -> discovered PUBLIC https result URLs
        -> strict URL + all-resolved-IP public-DNS vetting
        -> a recurring feed/page source in the ISOLATED adaptive registry

Safety (fail-closed, no fabricated coverage):

- Only ``https`` results on the default port, with no credentials and no
  secret-like query keys, are considered. Every resolved A/AAAA address for the
  hostname must be public (no private/loopback/link-local/reserved/multicast or
  IPv4-mapped addresses); a host that resolves to zero addresses or any private
  address is rejected *before* any fetch.
- A discovered page is untrusted EVIDENCE, never an instruction: nothing parsed
  from it can mutate a watch, plan, preference or permission.
- A backend that is empty/unsupported/rate-limited registers NOTHING and is
  reported honestly; no feed and no result URL is ever fabricated.
- Registration is bounded (``native_search_max_discovered_sources``) and tagged
  with provenance + watch id + plan version so a watch revision retires it.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable

from . import publicfeeds as pf
from . import publictransport as publictransport
from .planner import is_supported_scope
from .searchbridge import (
    STATUS_OK,
    SearchHit,
    SearchOutcome,
)

DISCOVERED_SCOPE = "public"
DEFAULT_MAX_DISCOVERED = 3
# A bounded candidate POOL larger than the registration cap lets the reconciler
# substitute a viable alternative when the top-ranked discovered sites are
# persistently unreachable. It never raises the number of LIVE discovered
# sources (that stays at ``native_search_max_discovered_sources``), never issues
# extra searches, and is itself hard-capped.
DEFAULT_POOL_MULTIPLIER = 3
MAX_DISCOVERED_POOL = 12
# Consecutive fetch failures after which a discovered source is quarantined (its
# slot rotates to a bounded alternative) until the backoff window elapses.
DEFAULT_FAILURE_THRESHOLD = 2
DEFAULT_BACKOFF_MINUTES = 360
MAX_FAILURE_RECORDS = 50
# Feed-looking hints determine the recurring route preferred for a result.
_FEED_HINTS = ("/feed", "/rss", "/atom", "/feeds/", ".rss", ".atom", ".xml",
               "format=rss", "format=atom", "rss.xml", "atom.xml")
_JSON_HINTS = ("/api/", ".json", "format=json")


class DiscoveryRejected(ValueError):
    """A discovered source failed vetting; it is never fetched or registered."""


# --------------------------------------------------------------------------- #
# Public-DNS / SSRF vetting
# --------------------------------------------------------------------------- #


def _ip_is_public(text: str) -> bool:
    """True only for a globally routable address (see publictransport)."""
    return publictransport.ip_is_global(text)


def resolve_public_ips(
    host: str,
    *,
    resolver: Callable[[str], list[str]] | None = None,
) -> list[str]:
    """Return the resolved addresses, failing closed when none/any is unsafe.

    Delegates to the pinned transport's global-address check (rejecting
    private/loopback/link-local/reserved/unspecified/mapped/CGNAT). ``resolver``
    is injectable so tests use a fake DNS table and never touch the network.
    """
    try:
        return publictransport.resolve_public_ips(host, resolver=resolver)
    except publictransport.TransportRejected as exc:
        raise DiscoveryRejected(str(exc)) from exc


def vet_discovered_url(
    url: str,
    *,
    resolver: Callable[[str], list[str]] | None = None,
) -> str:
    """Validate a dynamically discovered PUBLIC https URL end to end.

    Relaxes ONLY the static host/path allowlist (discovered hosts are unknown by
    construction); the scheme, credential, port, secret-query and all-resolved-IP
    checks are unchanged. Raises ``DiscoveryRejected`` on failure.
    """
    try:
        return publictransport.vet_public_url(url, resolver=resolver)
    except publictransport.TransportRejected as exc:
        raise DiscoveryRejected(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Classification: discovered URL -> recurring route
# --------------------------------------------------------------------------- #


def classify_hit(hit: SearchHit | dict[str, Any], *, content_type: str = "") -> dict[str, Any]:
    """Classify a vetted hit into a recurring feed/page route.

    Returns ``{"kind", "url", "interest"}``. Feed-looking URLs (RSS/Atom) use the
    RSS parser, JSON endpoints use the JSON parser, everything else uses the
    deterministic bounded web-page parser. The route kind is derived from the
    URL shape only; a page can never declare its own trusted kind at fetch time.
    """
    url = str((hit.get("url") if isinstance(hit, dict) else hit.url) or "").strip()
    title = str((hit.get("title") if isinstance(hit, dict) else hit.title) or "").strip()
    lowered = url.lower()
    ctype = str(content_type or "").lower()
    if any(hint in lowered for hint in _FEED_HINTS) or "rss" in ctype or "atom" in ctype or "xml" in ctype:
        kind = "rss"
    elif any(hint in lowered for hint in _JSON_HINTS) or "json" in ctype:
        kind = "web_json"
    else:
        kind = "web_page"
    return {"kind": kind, "url": url, "interest": title[:120]}


# --------------------------------------------------------------------------- #
# Plan -> discovered sources
# --------------------------------------------------------------------------- #


def _empty(status: str, reason: str) -> dict[str, Any]:
    return {"status": status, "reason": reason, "outcomes": [], "sources": [],
            "discovered": 0, "searched_queries": 0, "rejected": 0,
            "attempts": {"ok": 0, "empty": 0, "failed": 0}}


def discover_public_sources(
    plan: dict[str, Any],
    policy: dict[str, Any],
    bridge: Any,
    *,
    budget: Any = None,
    resolver: Callable[[str], list[str]] | None = None,
    now: datetime | None = None,
    overall_budget: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Search the broad public web for one plan and vet recurring sources.

    Returns ``(sources, summary)``. ``sources`` is empty unless at least one
    result genuinely vetted. A healthy-empty / unsupported / rate-limited backend
    returns NO sources and an explicit status. The per-cycle bridge budget bounds
    queries; a 429 stops the cycle immediately (never retried/bypassed). Each
    query subprocess timeout is clamped to the REMAINING overall collector budget
    so discovery can never overrun the whole run, and aggregate status honestly
    reflects mixed attempt outcomes (``partial`` for some failures plus results).
    """
    now = now or datetime.now(timezone.utc)
    scope = str(plan.get("scope") or "public")
    queries = [str(q) for q in (plan.get("queries") or [])]
    if not is_supported_scope(scope) or not queries:
        return [], _empty("unsupported", "no supported public route/query")
    if not bool(policy.get("adaptive_broad_discovery_enabled", False)):
        return [], _empty("disabled", "broad discovery not enabled by policy")

    max_sources = max(1, int(policy.get("native_search_max_discovered_sources",
                                       DEFAULT_MAX_DISCOVERED)))
    pool_cap = max(max_sources, min(
        int(policy.get("native_search_max_discovered_pool",
                       max_sources * DEFAULT_POOL_MULTIPLIER)),
        MAX_DISCOVERED_POOL))
    seen: set[str] = set()
    sources: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    rejected = 0
    searched = 0
    ok_attempts = 0
    empty_attempts = 0
    failed_attempts = 0
    stop_status = ""
    stop_reason = ""
    for query in queries:
        if budget is not None and budget.remaining() <= 0:
            stop_status = "budget"
            stop_reason = "per-cycle search budget exhausted"
            break
        # Clamp the per-query subprocess timeout to the remaining overall
        # collector budget so native search cannot outlive the run.
        query_timeout: float | None = None
        if overall_budget is not None:
            query_timeout = max(0.05, float(overall_budget.remaining()))
        if budget is not None:
            budget.take(1)
        try:
            if query_timeout is not None and _search_accepts_timeout(bridge):
                outcome = bridge.search(query, timeout=query_timeout)
            else:
                outcome = bridge.search(query)
        except Exception:  # noqa: BLE001 - one query never breaks discovery
            outcome = SearchOutcome(query=query, status="failed", error="search invocation failed")
        searched += 1
        outcomes.append(outcome.to_dict())
        if outcome.status == "rate_limited":
            stop_status = "rate_limited"
            stop_reason = "backend rate limited; cycle stopped"
            failed_attempts += 1
            break
        if outcome.ok:
            ok_attempts += 1
        elif outcome.status == "empty":
            empty_attempts += 1
        else:
            failed_attempts += 1
        if not outcome.ok:
            continue
        for hit in outcome.hits:
            if len(sources) >= pool_cap:
                break
            try:
                vetted_url = vet_discovered_url(hit.url, resolver=resolver)
            except DiscoveryRejected:
                rejected += 1
                continue
            key = vetted_url
            if key in seen:
                continue
            seen.add(key)
            classified = classify_hit(hit)
            classified["url"] = vetted_url
            classified["provenance"] = (
                f"broad public discovery for watch {plan.get('watch_id')} "
                f"(plan v{plan.get('watch_version')}; query={query!r})"
            )
            sources.append(classified)
    if sources:
        status = "partial" if failed_attempts else "ok"
        reason = stop_reason or ("some queries failed" if failed_attempts else "")
    elif stop_status:
        status = stop_status
        reason = stop_reason
    elif failed_attempts and not ok_attempts:
        status = "failed"
        reason = "all search attempts failed"
    elif failed_attempts:
        status = "failed"
        reason = "search attempts failed with no usable results"
    elif searched:
        status = "empty"
        reason = ""
    else:
        status = "ok"
        reason = ""
    summary = {
        "status": status, "reason": reason, "outcomes": outcomes,
        "sources": [{"kind": s["kind"], "host": pf.canonical_host(
            urllib.parse.urlsplit(s["url"]).hostname or ""), "url": s["url"]} for s in sources],
        "discovered": len(sources), "searched_queries": searched, "rejected": rejected,
        "attempts": {"ok": ok_attempts, "empty": empty_attempts, "failed": failed_attempts},
        "registration_cap": max_sources, "pool_cap": pool_cap,
    }
    return sources, summary


def _search_accepts_timeout(bridge: Any) -> bool:
    """True when a bridge advertises a bounded ``timeout`` search parameter."""
    try:
        import inspect

        sig = inspect.signature(bridge.search)
    except (TypeError, ValueError):
        return False
    return "timeout" in sig.parameters


def _failure_threshold(policy: dict[str, Any]) -> int:
    return max(1, int(policy.get("native_search_discovered_failure_threshold",
                                DEFAULT_FAILURE_THRESHOLD)))


def _backoff_minutes(policy: dict[str, Any]) -> int:
    return max(1, int(policy.get("native_search_discovered_backoff_minutes",
                                policy.get("discovery_cadence_minutes",
                                          DEFAULT_BACKOFF_MINUTES))))


def load_failure_ledger(registry: dict[str, Any]) -> dict[str, Any]:
    """Bounded URL -> failure state, surviving a quarantined record's removal."""
    ledger = registry.get("discovered_failures")
    if not isinstance(ledger, dict):
        ledger = {}
        registry["discovered_failures"] = ledger
    return ledger


def note_discovered_failure_state(
    registry: dict[str, Any], now: datetime,
) -> None:
    """Persist/clear failure state for every live discovered record.

    Called after the fetch phase so a failing source's count survives the
    reconciler dropping its slot, and a healthy fetch clears the backoff. Only
    the isolated adaptive registry is touched.
    """
    ledger = load_failure_ledger(registry)
    for rec in list(registry.get("sources", {}).values()):
        if not rec.get("discovered"):
            continue
        url = str(rec.get("url") or "")
        if not url:
            continue
        failures = int(rec.get("failures", 0) or 0)
        if failures <= 0:
            ledger.pop(url, None)
        else:
            prior = ledger.get(url) if isinstance(ledger.get(url), dict) else {}
            ledger[url] = {
                "failures": max(failures, int(prior.get("failures", 0) or 0)),
                "last_failure_at": str(rec.get("last_fetch_at") or now.isoformat()),
            }
    # Bound the ledger, dropping the oldest failure entries first.
    if len(ledger) > MAX_FAILURE_RECORDS:
        oldest = sorted(ledger.items(), key=lambda kv: str(kv[1].get("last_failure_at", "")))
        for url, _ in oldest[:len(ledger) - MAX_FAILURE_RECORDS]:
            ledger.pop(url, None)


def _discovered_quarantined(
    url: str, rec: dict[str, Any] | None, ledger: dict[str, Any],
    now: datetime, policy: dict[str, Any],
) -> bool:
    """True when a discovered source failed enough and is still in backoff."""
    state = ledger.get(url) if isinstance(ledger.get(url), dict) else {}
    record_failures = int((rec or {}).get("failures", 0) or 0)
    failures = max(int(state.get("failures", 0) or 0), record_failures)
    if failures < _failure_threshold(policy):
        return False
    last = str(state.get("last_failure_at") or (rec or {}).get("last_fetch_at") or "")
    if not last:
        return False
    return pf._age_minutes(now, last) < _backoff_minutes(policy)


def ensure_discovered_sources(
    registry: dict[str, Any],
    plan: dict[str, Any],
    sources: list[dict[str, Any]],
    policy: dict[str, Any],
    now: datetime,
    *,
    resolver: Callable[[str], list[str]] | None = None,
) -> int:
    """Reconcile discovered sources for one watch into the adaptive registry.

    A plan revision retires superseded queries/pages. Within one revision the
    LIVE set is bounded by ``native_search_max_discovered_sources`` (never
    increased); a source that has failed past the threshold is quarantined for a
    bounded backoff and its slot is handed to an alternative from the bounded
    discovery pool, so the top-ranked-but-unreachable results can never
    permanently starve other viable sources. Existing records keep their cache
    and failure counters across cycles. Every record is marked ``vetted_public``
    so the collection path may fetch it despite the static allowlist, and carries
    watch id + plan version provenance.
    """
    wid = str(plan.get("watch_id") or "")
    version = int(plan.get("watch_version", 0) or 0)
    cap = max(1, int(policy.get("native_search_max_discovered_sources",
                               DEFAULT_MAX_DISCOVERED)))
    limit = int(policy.get("adaptive_registry_limit", pf.DEFAULT_REGISTRY_LIMIT))
    ledger = load_failure_ledger(registry)

    # Snapshot the vetted in-order pool. Re-vet to be safe; a rejected URL is
    # never registered.
    pool: list[dict[str, Any]] = []
    for src in sources:
        url = str(src.get("url") or "")
        try:
            vet_discovered_url(url, resolver=resolver)
        except DiscoveryRejected:
            continue
        pool.append(src)

    # Existing discovered records for THIS revision (keep cache/failure state).
    existing: dict[str, tuple[str, dict[str, Any]]] = {
        str(rec.get("url") or ""): (sid, rec)
        for sid, rec in registry.get("sources", {}).items()
        if str(rec.get("watch_id") or "") == wid and rec.get("discovered")
        and int(rec.get("plan_version", -1) or 0) == version
        and not rec.get("parked")
    }

    chosen: list[tuple[str, dict[str, Any]]] = []  # (url, src-descriptor)
    seen: set[str] = set()

    # 1. Pool order, skipping quarantined sources, preferring existing records.
    for src in pool:
        url = str(src.get("url") or "")
        if not url or url in seen:
            continue
        rec = existing.get(url, (None, None))[1]
        if _discovered_quarantined(url, rec, ledger, now, policy):
            continue
        seen.add(url)
        chosen.append((url, src))
        if len(chosen) >= cap:
            break

    # 2. Only if the whole pool is quarantined/empty, re-admit the least-failed
    #    quarantined existing records so a transient outage never goes dark.
    if len(chosen) < cap:
        for url, (sid, rec) in sorted(
            existing.items(), key=lambda kv: int(kv[1][1].get("failures", 0) or 0)
        ):
            if url in seen:
                continue
            seen.add(url)
            chosen.append((url, {"kind": rec.get("kind"), "url": url,
                                 "interest": rec.get("interest"),
                                 "provenance": rec.get("provenance")}))
            if len(chosen) >= cap:
                break

    # 3. Remove every non-chosen discovered record for the watch (retires old
    #    revisions and quarantined alternatives).
    keep_sids = {existing[url][0] for url, _ in chosen if url in existing}
    for sid, rec in list(registry.get("sources", {}).items()):
        if (str(rec.get("watch_id") or "") == wid and rec.get("discovered")
                and sid not in keep_sids and not rec.get("parked")):
            registry["sources"].pop(sid, None)

    # 4. Refresh/insert the chosen records (bounded by the registry limit).
    added = 0
    for url, src in chosen:
        if len(registry.get("sources", {})) >= max(1, limit) and url not in existing:
            break
        kind = str(src.get("kind") or "web_page")
        sid = pf._source_id(kind, url)
        existing_rec = existing.get(url, (None, None))[1]
        if existing_rec is not None:
            existing_rec["plan_version"] = version
            existing_rec["provenance"] = str(src.get("provenance")
                                             or existing_rec.get("provenance") or "")
            existing_rec["kind"] = kind
            if str(src.get("interest") or ""):
                existing_rec["interest"] = str(src.get("interest"))
            registry["sources"][sid] = existing_rec
            added += 1
            continue
        host = pf.canonical_host(urllib.parse.urlsplit(url).hostname or "")
        registry.setdefault("sources", {})[sid] = {
            "id": sid,
            "kind": kind,
            "url": url,
            "host": host,
            "provenance": str(src.get("provenance") or "broad public discovery"),
            "interest": str(src.get("interest") or ""),
            "tentative": True,
            "auto_added": True,
            "discovered": True,
            "vetted_public": True,
            "watch_id": wid,
            "plan_version": version,
            "cadence_minutes": max(1, int(policy.get(
                "discovery_cadence_minutes", policy.get("adaptive_cadence_minutes", 360)))),
            "added_at": now.isoformat(),
            "last_fetch_at": "",
            "etag": "",
            "last_modified": "",
            "cache_at": "",
            "cache_items": [],
            "failures": 0,
        }
        added += 1
    registry["updated_at"] = now.isoformat()
    return added
