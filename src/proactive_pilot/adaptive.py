"""Bounded adaptive acquisition on EXISTING approved public routes.

This module turns an explicit, validated watch plan into a small number of
public searches using only the already-approved endpoints and host/path
validation from :mod:`proactive_pilot.publicfeeds` (GitHub search, Reddit
search). It never enables an arbitrary host, never follows a discovered link
(links are evidence only), never reads a private source and never writes to the
ordinary public registry or notification state.

Isolation:
- Adaptive state lives in its own artifacts (``adaptive_registry.json``,
  ``adaptive_coverage.json``); ``public_registry.json`` and ``notify_state.json``
  are untouched.
- Results are returned as ordinary ``SourceResult`` objects so the parent can,
  later and explicitly, feed them through the existing feed packet gates. This
  module itself never emits anything to stdout.

Honesty:
- A scope with no supported public route is recorded as ``unsupported`` -- never
  as a healthy empty result and never as "nothing found".
- A query is only ever fetched on the current, validated plan for its watch, so
  changing a watch retires its old queries.
- Untrusted feed text can never mutate a watch or the plan.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from . import publicfeeds as pf
from .collectors import Budget, SourceResult
from .planner import is_supported_scope, plan_for_watch
from .watchstore import (
    active_watches,
    load_watchstore,
    save_watchstore,
    watch_is_retired,
    watch_lock,
)

ADAPTIVE_REGISTRY = "adaptive_registry.json"
ADAPTIVE_COVERAGE = "adaptive_coverage.json"
COVERAGE_SCHEMA = 1
MAX_COVERAGE_WATCHES = 20
# A whole-number-of-seconds floor below which model planning is DEFERRED to the
# honest deterministic fallback rather than risk overrunning the collector.
PLAN_DEFER_MIN_SECONDS = 1.0


def _default_pinned_fetcher(resolver: Any = None) -> Any:
    """Default fetcher: a DNS-pinned public HTTPS transport (SSRF/rebinding safe).

    Falls back to the ordinary fetcher only if the pinned transport cannot be
    imported, so a discovered source is never silently fetched through a generic
    opener that could re-resolve to a private address.
    """
    try:
        from .publictransport import PinnedHTTPSTransport

        return PinnedHTTPSTransport(resolver=resolver).fetch
    except Exception:  # noqa: BLE001 - never block acquisition on optional module
        return pf.fetch_bytes


class PublicAttemptLedger:
    """One shared source-attempt allowance for ordinary + adaptive public work.

    Ordinary feeds and adaptive evidence draw from the SAME allowance so the
    combined HTTP attempts per cycle never exceed ``feed_max_sources_per_run``
    (never 2 ordinary PLUS 2 adaptive). Which path gets first refusal rotates per
    cycle (see :func:`next_public_rotation`) so an enabled adaptive watch is not
    forever starved by the ordinary feeds. The ordinary-only baseline (adaptive
    disabled) never constructs this ledger, so its behaviour is unchanged.
    """

    def __init__(self, total: int, *, adaptive_first: bool = False):
        self.total = max(0, int(total))
        self.used = 0
        self.adaptive_first = bool(adaptive_first)

    def remaining(self) -> int:
        return max(0, self.total - self.used)

    def take(self, want: int) -> int:
        got = min(self.remaining(), max(0, int(want)))
        self.used += got
        return got


def next_public_rotation(store: Any) -> bool:
    """Return whether adaptive gets first refusal this cycle (and advance it).

    The cursor is stored in the isolated adaptive registry; it never touches the
    ordinary registry. Alternating priority keeps both paths from starving the
    other while the combined attempt cap stays fixed.
    """
    registry = load_adaptive_registry(store)
    cycle = int(registry.get("public_cycle", 0) or 0)
    registry["public_cycle"] = cycle + 1
    try:
        save_adaptive_registry(store, registry)
    except Exception:  # noqa: BLE001 - rotation must never block a run
        pass
    return (cycle % 2) == 1


INFERRED_FAIR_CURSOR = "inferred_fair_cursor"


def _inferred_fair_period(policy: dict[str, Any]) -> int:
    # Never a permanent explicit-only rotation: at least every 2nd slot.
    return max(2, int(policy.get("adaptive_inferred_fairness_period", 3) or 3))


def inferred_turn(store: Any, policy: dict[str, Any]) -> bool:
    """Durable bounded fairness between explicit watches and inferred discovery.

    Explicit watches keep priority, but every ``period``-th planning slot is
    offered to inferred discovery, so no number of saved explicit watches can
    permanently disable adaptation (AUTONOMOUS_SCOPE requirement 4). The cursor
    lives in the ISOLATED adaptive registry and advances exactly once per call,
    so the fairness survives a restart instead of resetting to explicit-only.
    Returns False when inferred goals are disabled.
    """
    if not _inferred_enabled(policy):
        return False
    period = _inferred_fair_period(policy)
    registry = load_adaptive_registry(store)
    cursor = int(registry.get(INFERRED_FAIR_CURSOR, 0) or 0)
    registry[INFERRED_FAIR_CURSOR] = cursor + 1
    try:
        save_adaptive_registry(store, registry)
    except Exception:  # noqa: BLE001 - fairness must never block a run
        pass
    # Offsets so the FIRST slot is explicit (explicit priority), then the
    # period-th slot is inferred-eligible.
    return (cursor % period) == (period - 1)


def new_adaptive_registry() -> dict[str, Any]:
    return {"schema": 1, "updated_at": "", "feed_cursor": 0, "sources": {}}


def load_adaptive_registry(store: Any) -> dict[str, Any]:
    data = store.load(ADAPTIVE_REGISTRY, new_adaptive_registry())
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return new_adaptive_registry()
    return data


def save_adaptive_registry(store: Any, registry: dict[str, Any]) -> None:
    store.save(ADAPTIVE_REGISTRY, registry)


def new_coverage() -> dict[str, Any]:
    return {"schema": COVERAGE_SCHEMA, "updated_at": "", "watches": {}}


def load_coverage(store: Any) -> dict[str, Any]:
    data = store.load(ADAPTIVE_COVERAGE, new_coverage())
    if not isinstance(data, dict) or not isinstance(data.get("watches"), dict):
        return new_coverage()
    return data


# --------------------------------------------------------------------------- #
# Plan preparation (BEFORE any HTTP phase)
# --------------------------------------------------------------------------- #


def _inferred_enabled(policy: dict[str, Any]) -> bool:
    return bool(policy.get("adaptive_inferred_goals_enabled", False))


def prepare_inferred_plan(
    policy: dict[str, Any],
    clock: Any,
    *,
    store: Any,
    planner: Any = None,
    context: list[str] | None = None,
    interest_proposer: Any = None,
    now: datetime | None = None,
    force: bool = False,
    overall_budget: Any = None,
    privacy_verifier: Any = None,
) -> dict[str, Any] | None:
    """Prepare ONE inferred goal (BMODEL proposal/cache + query plan).

    Called under the SAME ``watch_lock`` as the explicit path. Returns a
    ``prepared`` dict with ``origin:"inferred"`` or ``None`` when no goal can be
    selected (disabled, caps/cooldown/backoff, corrupt state, empty context).
    Never writes the explicit watch store.

    Budget honesty: the shared remaining budget for proposal+resolution+planning
    is computed BEFORE the proposer runs and clamped to
    ``overall.remaining() - (public + obligation)``. When it is near zero the
    model proposer is not called at all (only safe cache reuse); when it depletes
    during the proposer the planner is deferred with NO budget reset.
    """
    from . import interestproposal as ip

    started = time.monotonic()
    now = now or clock.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        data = ip.load_inferred_registry(store, strict=True)
    except Exception:  # noqa: BLE001 - corrupt inferred state fails closed (no model/read)
        return None
    ip.sweep_expired(data, now)
    # Compute the shared proposal+planning allowance BEFORE any proposer call so
    # the proposer can never start when real acquisition's reservation is at
    # risk. reserve = public 10 + obligation 15 (never additive).
    reserve = (float(policy.get("public_budget_seconds", 10))
               + float(policy.get("obligation_budget_seconds", 15)))
    stage_fit: float | None = None
    effective_proposer = interest_proposer
    if overall_budget is not None:
        stage_fit = float(overall_budget.remaining()) - reserve
        if stage_fit <= PLAN_DEFER_MIN_SECONDS:
            # Near-zero fit: ZERO proposer/resolver calls; reuse a cached goal
            # only (safe, no model, no new state) if one is ready.
            effective_proposer = None
    proposal_policy = policy
    if effective_proposer is not None and stage_fit is not None:
        proposal_policy = dict(policy)
        proposal_policy["_inferred_stage_deadline"] = time.monotonic() + max(0.0, stage_fit)
    # ``force`` refreshes the PLAN cache only; goal selection always honours the
    # proposal cooldown/daily caps so a forced plan refresh can never spam the
    # model or exceed the inferred-goal budget.
    goal = ip.select_goal(data, proposal_policy, now, context=context,
                          proposer=effective_proposer,
                          privacy_verifier=privacy_verifier)
    # Purge evidence for anything no longer legitimate (expired/superseded/
    # revoked, or active-but-preference-blocked) so a revised/blocked goal can
    # never replay obsolete evidence.
    purge_inactive_inferred_evidence(store, data)
    ip.save_inferred_registry(store, data)
    if goal is None:
        return None

    watch = ip.goal_to_watch(goal)
    # Re-run the enforced privacy boundary on the plan queries before any
    # external search/fetch; structural + optional semantic review.
    plan_timeout = float(policy.get("adaptive_plan_timeout_seconds", 60))
    planning_deferred = False
    effective_planner = planner
    if overall_budget is not None:
        # Recompute AFTER the proposer: its elapsed time has already reduced the
        # shared remaining budget, so a depleting fit defers the planner without
        # any reset/refill.
        fit = float(overall_budget.remaining()) - reserve
        if fit <= PLAN_DEFER_MIN_SECONDS:
            planning_deferred = True
            effective_planner = None
        else:
            plan_timeout = min(plan_timeout, fit)
    if effective_planner is None and planner is not None:
        planning_deferred = True
    plan, used_model = plan_for_watch(watch, policy, effective_planner, now=now,
                                      force=force, plan_timeout=plan_timeout)
    plan["origin"] = "inferred"
    plan["privacy_revision"] = str(goal.get("privacy_revision") or "")
    plan["context_revision"] = str(goal.get("context_revision") or "")
    _rescreen_inferred_plan_queries(plan, context)
    # Persist the plan on the goal so the next cycle reuses it (cadence/cache).
    data = ip.load_inferred_registry(store, strict=True)
    stored = data.get("goals", {}).get(str(goal.get("id")))
    if isinstance(stored, dict):
        stored["plan"] = plan
        stored["plan_version"] = int(plan.get("watch_version", stored.get("version", 1)) or 1)
        stored["plan_at"] = str(plan.get("planned_at") or now.isoformat())
        stored["updated_at"] = now.isoformat()
        ip.save_inferred_registry(store, data)
    return {"status": "prepared", "reason": "", "watch": watch, "plan": plan,
            "used_model": bool(used_model), "planning_deferred": planning_deferred,
            "plan_elapsed": time.monotonic() - started, "origin": "inferred",
            "goal_id": str(goal.get("id") or "")}


def prepare_adaptive_plan(
    policy: dict[str, Any],
    clock: Any,
    *,
    store: Any,
    planner: Any = None,
    context: list[str] | None = None,
    interest_proposer: Any = None,
    now: datetime | None = None,
    paused: bool = False,
    force: bool = False,
    overall_budget: Any = None,
    privacy_verifier: Any = None,
) -> dict[str, Any]:
    """Plan (or reuse the cache for) at most ONE watch BEFORE HTTP starts.

    This is the explicit plan/cache refresh split out of acquisition so the
    zero-tool model phase can never consume the shared public HTTP budget and so
    the plan is ready before ordinary feeds issue their first request. It runs
    under the SAME non-blocking ``watch_lock`` and honours the global pause.

    The model phase is bounded by a SEPARATE planner timeout AND clamped to the
    remaining overall collector budget (minus the still-to-run public/obligation
    allocations). When planning cannot fit, the model phase is DEFERRED to the
    honest deterministic fallback and reported as such -- budgets are never
    expanded and no HTTP request is ever made here.
    """
    started = time.monotonic()
    now = now or clock.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if paused:
        return {"status": "skipped", "reason": "global pause active", "watch": None,
                "plan": None, "used_model": False, "planning_deferred": False,
                "plan_elapsed": time.monotonic() - started}

    with watch_lock(store) as acquired:
        if not acquired:
            return {"status": "busy", "reason": "watch store locked by another writer",
                    "watch": None, "plan": None, "used_model": False,
                    "planning_deferred": False, "plan_elapsed": time.monotonic() - started}
        data = load_watchstore(store, strict=True)
        for wid, watch in list(data.get("watches", {}).items()):
            if watch_is_retired(watch, now):
                purge_watch_evidence(store, wid)
        active = sorted(active_watches(data, now), key=lambda w: str(w.get("id")))
        if not active:
            # ZERO explicit watches: the default-off inferred-goals stage runs
            # every slot (no contention to ration).
            if _inferred_enabled(policy):
                inferred = prepare_inferred_plan(
                    policy, clock, store=store, planner=planner, context=context,
                    interest_proposer=interest_proposer, now=now, force=force,
                    overall_budget=overall_budget, privacy_verifier=privacy_verifier)
                if inferred is not None:
                    inferred["plan_elapsed"] = time.monotonic() - started
                    return inferred
            return {"status": "no_active_watch", "reason": "no active watch", "watch": None,
                    "plan": None, "used_model": False, "planning_deferred": False,
                    "plan_elapsed": time.monotonic() - started}
        # Explicit watches keep priority, but a bounded durable fair rotation
        # offers every period-th slot to inferred discovery so a saved explicit
        # watch can never permanently disable adaptation. Falls back to the
        # explicit watch when the inferred stage has nothing ready.
        if _inferred_enabled(policy) and inferred_turn(store, policy):
            inferred = prepare_inferred_plan(
                policy, clock, store=store, planner=planner, context=context,
                interest_proposer=interest_proposer, now=now, force=force,
                overall_budget=overall_budget, privacy_verifier=privacy_verifier)
            if inferred is not None:
                inferred["plan_elapsed"] = time.monotonic() - started
                return inferred
        cursor = int(data.get("cursor", 0)) % len(active)
        watch = active[cursor]
        data["cursor"] = (cursor + 1) % len(active)

        plan_timeout = float(policy.get("adaptive_plan_timeout_seconds", 60))
        planning_deferred = False
        effective_planner = planner
        if overall_budget is not None:
            reserve = (float(policy.get("public_budget_seconds", 10))
                       + float(policy.get("obligation_budget_seconds", 15)))
            fit = overall_budget.remaining() - reserve
            if fit <= PLAN_DEFER_MIN_SECONDS:
                planning_deferred = True
                effective_planner = None
            else:
                plan_timeout = min(plan_timeout, fit)
        if effective_planner is None and planner is not None:
            planning_deferred = True
        plan, used_model = plan_for_watch(watch, policy, effective_planner, now=now,
                                          force=force, plan_timeout=plan_timeout)
        watch["plan"] = plan
        watch["plan_version"] = int(plan.get("watch_version", watch.get("version", 0)) or 0)
        watch["plan_at"] = str(plan.get("planned_at") or now.isoformat())
        save_watchstore(store, data)
    return {"status": "prepared", "reason": "", "watch": watch, "plan": plan,
            "used_model": bool(used_model), "planning_deferred": planning_deferred,
            "plan_elapsed": time.monotonic() - started}


# --------------------------------------------------------------------------- #
# Plan -> supported route URLs
# --------------------------------------------------------------------------- #


def query_urls(query: str, scope: str) -> list[tuple[str, str]]:
    """Map one validated query to approved public route URLs."""
    quoted = urllib.parse.quote_plus(query)
    urls: list[tuple[str, str]] = []
    if scope != "github":
        urls.append(("reddit_search",
                     f"https://www.reddit.com/search.rss?q={quoted}&sort=new&t=week"))
    if scope != "reddit":
        urls.append(("github_search",
                     f"https://api.github.com/search/repositories?q={quoted}+in%3Aname%2Cdescription&sort=updated"))
    return urls


def plan_sources(plan: dict[str, Any]) -> list[dict[str, Any]]:
    scope = str(plan.get("scope") or "public")
    if not is_supported_scope(scope):
        return []
    out: list[dict[str, Any]] = []
    label = "inferred goal" if str(plan.get("origin") or "") == "inferred" else "adaptive watch"
    for query in plan.get("queries", []):
        for kind, url in query_urls(str(query), scope):
            out.append({
                "kind": kind,
                "url": url,
                "interest": str(query),
                "scope": scope,
                "provenance": f"{label} {plan.get('watch_id')} "
                              f"(plan v{plan.get('watch_version')}; {plan.get('provenance')})",
            })
    return out


def ensure_adaptive_sources(
    registry: dict[str, Any], plan: dict[str, Any], policy: dict[str, Any], now: datetime
) -> int:
    """Reconcile the adaptive registry to the current plan for this watch.

    Every prior source tagged with this watch id is removed first, so a watch
    revision retires its old queries. Adding is bounded and rotates the oldest
    auto-added source when full. Returns the number of live sources for the
    watch.
    """
    wid = str(plan.get("watch_id") or "")
    desired = plan_sources(plan)
    desired_keys = {(s["kind"], s["url"]) for s in desired}
    # Dynamically discovered sources for this watch are owned by the discovery
    # reconciler; a plan-route reconcile must never wipe them.
    existing = [rec for rec in registry.get("sources", {}).values()
                if str(rec.get("watch_id") or "") == wid and not rec.get("discovered")]
    existing_keys = {(str(rec.get("kind") or ""), str(rec.get("url") or "")) for rec in existing}
    # Idempotent reconcile: when the same watch revision still maps to the same
    # route set, keep the existing records so cadence/cache are preserved. A
    # revision (or a retired query) changes the set and triggers replacement.
    if existing and existing_keys == desired_keys and all(
        int(rec.get("plan_version", -1)) == int(plan.get("watch_version", 0) or 0)
        for rec in existing
    ):
        return len(existing)
    for sid in [key for key, rec in registry.get("sources", {}).items()
                if str(rec.get("watch_id") or "") == wid and not rec.get("discovered")]:
        registry["sources"].pop(sid, None)
    limit = int(policy.get("adaptive_registry_limit", pf.DEFAULT_REGISTRY_LIMIT))
    added = 0
    for src in desired:
        ok = pf.registry_add(
            registry,
            kind=src["kind"],
            url=src["url"],
            provenance=src["provenance"],
            interest=src["interest"],
            now=now,
            cadence_minutes=int(policy.get("adaptive_cadence_minutes",
                                           policy.get("discovery_cadence_minutes", 360))),
            limit=limit,
            generic_opt_in=False,
        )
        if not ok:
            continue
        sid = pf._source_id(src["kind"], src["url"])
        rec = registry["sources"].get(sid)
        if isinstance(rec, dict):
            rec["watch_id"] = wid
            rec["plan_version"] = int(plan.get("watch_version", 0) or 0)
            rec["scope"] = src["scope"]
            # Explicit origin label so ordinary packet evidence can distinguish
            # an inferred discovery from a user-requested watch.
            rec["origin"] = str(plan.get("origin") or "")
            added += 1
    registry["updated_at"] = now.isoformat()
    return added


# --------------------------------------------------------------------------- #
# Acquisition
# --------------------------------------------------------------------------- #


def _coverage_row(rec: dict[str, Any], result: SourceResult | None, *, status: str,
                  reason: str = "") -> dict[str, Any]:
    host = str(rec.get("host") or "")
    row = {
        "kind": str(rec.get("kind") or ""),
        "host": host,
        "interest": str(rec.get("interest") or ""),
        "status": status,
        "checked_at": "",
        "count": 0,
        "error": "",
    }
    if result is not None:
        row["checked_at"] = str(result.checked_at or result.cache_at or "")
        row["count"] = int(result.count)
        row["error"] = str(result.error or result.failure or "")
        if result.status == "ok" and result.truncated:
            row["status"] = "truncated"
        elif result.status == "ok":
            row["status"] = "success"
        elif result.status == "stale":
            row["status"] = "failed"
        else:
            row["status"] = "failed"
    if reason:
        row["reason"] = reason
    return row


def _due(rec: dict[str, Any], now: datetime) -> bool:
    last = str(rec.get("last_fetch_at") or "")
    if not last:
        return True
    return pf._age_minutes(now, last) >= int(rec.get("cadence_minutes") or 1)


def purge_watch_evidence(store: Any, watch_id: str) -> int:
    """Remove all adaptive registry/coverage evidence for one watch id.

    Called on stop/forget and lazily for expired watches, so a retired watch can
    never replay or inject obsolete evidence. Touches only the isolated adaptive
    artifacts. Returns the number of registry sources removed.
    """
    wid = str(watch_id)
    registry = load_adaptive_registry(store)
    removed = 0
    for sid in [key for key, rec in registry.get("sources", {}).items()
                if str(rec.get("watch_id") or "") == wid]:
        registry["sources"].pop(sid, None)
        removed += 1
    if removed:
        save_adaptive_registry(store, registry)
    coverage = load_coverage(store)
    if wid in coverage.get("watches", {}):
        coverage["watches"].pop(wid, None)
        store.save(ADAPTIVE_COVERAGE, coverage)
    # Retire the persisted native-search cache/backoff for this watch too, so a
    # stopped/forgotten watch cannot replay a cached result on a later cycle.
    try:
        from .searchbridge import purge_cached_watch

        purge_cached_watch(store, wid)
    except Exception:  # noqa: BLE001 - cleanup never blocks lifecycle
        pass
    return removed


def purge_inactive_inferred_evidence(store: Any, data: dict[str, Any]) -> list[str]:
    """Purge evidence a goal may no longer legitimately acquire with.

    Expired, revoked and superseded goals must not leave cached evidence behind.
    A still-ACTIVE but preference/snooze-blocked goal must ALSO have its evidence
    purged (it must not replay cached results while suppressed) WITHOUT being
    revoked: when the block lifts, ``preference_purged`` is cleared so it can
    re-acquire. Idempotent via the per-goal purge markers; touches only isolated
    adaptive artifacts. Returns purged goal ids.
    """
    purged: list[str] = []
    for gid, goal in list(data.get("goals", {}).items()):
        if not isinstance(goal, dict):
            continue
        if str(goal.get("status")) == "active":
            if goal.get("preference_blocked"):
                # Purge once per blocking episode; keep the goal active.
                if not goal.get("preference_purged"):
                    purge_watch_evidence(store, str(gid))
                    goal["preference_purged"] = True
                    purged.append(str(gid))
            else:
                # Unblocked: allow a future block to purge again.
                goal["preference_purged"] = False
            continue
        if goal.get("evidence_purged"):
            continue
        purge_watch_evidence(store, str(gid))
        goal["evidence_purged"] = True
        purged.append(str(gid))
    return purged


def _rescreen_inferred_plan_queries(plan: dict[str, Any], context: list[str] | None) -> None:
    """Re-run the enforced structural privacy boundary on the FINAL queries.

    A cached plan or a model output must never carry an unsafe query to external
    search/fetch. A query that fails is dropped; if none survive the plan is
    explicitly unsupported (never a healthy empty search).
    """
    from . import interestproposal as ip

    original = list(plan.get("queries") or [])
    safe_queries: list[str] = []
    for query in original:
        try:
            ip._screen_public_safe(str(query), [], [])
            ip._screen_context_copy(str(query), [], [], context)
        except ip.InferredProposalError:
            continue
        safe_queries.append(str(query))
    if len(safe_queries) != len(original):
        plan["queries"] = safe_queries
        if not safe_queries:
            plan["unsupported_reason"] = "query failed the privacy boundary; rejected"


def _raw_from_row(rec: dict[str, Any], row: dict[str, Any]) -> SourceResult:
    """Build a partial, non-fresh SourceResult for a source that was not fetched.

    A skipped/deferred source is reported partial (truncated) rather than as a
    healthy empty check, so integration cannot mistake it for fresh evidence.
    """
    name = f"feed:{rec.get('id','unknown')}"
    checked = str(row.get("checked_at") or "")
    return SourceResult(name, "ok", count=0, items=[], checked_at=checked,
                        truncated=True, cached=bool(checked), cache_at=checked,
                        failure=str(row.get("reason") or row.get("error") or "not fetched"))


def _tag_inferred_items(results: list[SourceResult]) -> None:
    """Stamp returned packet items with an honest inferred origin label.

    Inferred goals live in a separate registry and must never be presented to the
    evaluator as a user request; the ordinary packet surfaces this origin.
    """
    for res in results:
        items = getattr(res, "items", None)
        if not items:
            continue
        # Work on shallow copies so the persisted source cache is never mutated
        # (the same dicts back ``rec['cache_items']``).
        tagged: list[Any] = []
        for item in items:
            if isinstance(item, dict):
                copy = dict(item)
                copy.setdefault("origin", "inferred")
                tagged.append(copy)
            else:
                tagged.append(item)
        res.items = tagged


def _empty_summary(status: str, reason: str, *, used_model: bool = False) -> dict[str, Any]:
    return {"status": status, "reason": reason, "planned": [], "sources": [],
            "planned_watches": 0, "used_model": bool(used_model), "results": [],
            "pipeline_status": "not_started", "coverage_status": "none"}


def _coverage_status(sources: list[dict[str, Any]]) -> str:
    """Classify SOURCE coverage independently of whether the pipeline ran.

    ``complete`` means every attempted source was read; ``partial`` means some
    succeeded and some failed/were skipped; ``unavailable`` means none was
    readable (e.g. a WAF/403) though the pipeline completed. A healthy pipeline
    with zero readable sources is still only ``unavailable`` coverage -- it is
    never reported as complete.
    """
    if not sources:
        return "none"
    ok = failed = skipped = 0
    for row in sources:
        status = str(row.get("status") or "")
        if status in {"success", "truncated"}:
            ok += 1
        elif status in {"failed", "stale"} or str(row.get("error") or ""):
            failed += 1
        else:
            skipped += 1
    if ok and (failed or skipped):
        return "partial"
    if ok:
        return "complete"
    if failed or skipped:
        return "unavailable"
    return "none"


def collect_adaptive(
    policy: dict[str, Any],
    clock: Any,
    *,
    store: Any,
    planner: Any = None,
    fetcher: Any = None,
    now: datetime | None = None,
    budget: Budget | None = None,
    restart_budget: bool = False,
    paused: bool = False,
    force_plan: bool = False,
    attempt_ledger: Any = None,
    overall_budget: Any = None,
    prepared: dict[str, Any] | None = None,
    search_bridge: Any = None,
    search_budget: Any = None,
    resolver: Any = None,
    reddit_ledger: Any = None,
    interest_proposer: Any = None,
    context: list[str] | None = None,
    privacy_verifier: Any = None,
) -> dict[str, Any]:
    """Run at most ONE adaptive watch acquisition and return a redacted summary.

    ``paused`` is absolute: no watch is read, no plan is made and no request is
    issued. Every watch-store read-modify-write (including the plan-cache write
    and cursor advance) happens under the SAME non-blocking ``watch_lock`` as the
    typed CLI, so an intervening user edit can never be overwritten by an old
    snapshot. When the lock is busy the run returns ``busy`` with zero planning
    and zero network.

    Budget honesty: planning is a SEPARATE bounded phase and never consumes the
    shared HTTP budget. Standalone callers get the planning phase excluded from
    the supplied budget; the main integration passes ``prepared`` (from
    :func:`prepare_adaptive_plan`, run before HTTP started) so no budget is
    restarted or refilled here. Each fetch timeout is clamped to the REMAINING
    HTTP budget, and ``attempt_ledger`` (when supplied) caps the combined
    ordinary+adaptive source attempts for the cycle.

    ``summary["results"]`` carries the real ``SourceResult`` objects for the
    optional, still-disabled, packet integration path.
    """
    now = now or clock.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    fetcher = fetcher or _default_pinned_fetcher(resolver)
    if paused:
        coverage = load_coverage(store)
        coverage["updated_at"] = now.isoformat()
        store.save(ADAPTIVE_COVERAGE, coverage)
        return _empty_summary("skipped", "global pause active")

    if prepared is not None and prepared.get("status") != "prepared":
        status = str(prepared.get("status") or "skipped")
        reason = str(prepared.get("reason") or "")
        if status == "busy":
            return _empty_summary("busy", reason or "watch store locked by another writer")
        if status == "no_active_watch":
            return _empty_summary("no_active_watch", reason or "no active watch")
        return _empty_summary("skipped", reason or "planning not prepared")

    planning_deferred = bool((prepared or {}).get("planning_deferred", False))
    plan_started = time.monotonic()
    with watch_lock(store) as acquired:
        if not acquired:
            return _empty_summary("busy", "watch store locked by another writer")

        data = load_watchstore(store, strict=True)
        # Retired (stopped/expired) watches never replay or inject evidence.
        for wid, watch in list(data.get("watches", {}).items()):
            if watch_is_retired(watch, now):
                purge_watch_evidence(store, wid)
        coverage = load_coverage(store)

        inferred_goal = False
        if prepared is not None:
            plan = dict(prepared.get("plan") or {})
            used_model_any = bool(prepared.get("used_model"))
            watch_id = str(plan.get("watch_id") or "")
            if str(prepared.get("origin") or "") == "inferred":
                # Inferred goals live in their own registry, NEVER in the watch
                # store. Re-validate the persisted goal (active + same version)
                # so a revoke/expiry between planning and acquisition fails closed.
                from . import interestproposal as ip
                idata = ip.load_inferred_registry(store, strict=True)
                current = idata.get("goals", {}).get(watch_id)
                if (not isinstance(current, dict) or not ip.goal_is_active(current, now)
                        or int(current.get("version", 0) or 0)
                        != int((prepared.get("watch") or {}).get("version", 0) or 0)):
                    return _empty_summary("deferred", "inferred goal changed between planning and acquisition")
                if ip.goal_blocked_by_preference(current, policy, now):
                    # A suppression/snooze added between planning and acquisition
                    # must never reach HTTP; fail closed with zero fetch.
                    return _empty_summary("deferred", "inferred goal blocked by preference")
                watch = ip.goal_to_watch(current)
                watch["plan"] = plan
                inferred_goal = True
            else:
                current = data.get("watches", {}).get(watch_id)
                # A user edit/retirement between planning and acquisition must fail
                # closed rather than fetch a stale plan or resurrect a stopped watch.
                if (not isinstance(current, dict) or watch_is_retired(current, now)
                        or int(current.get("version", 0) or 0)
                        != int((prepared.get("watch") or {}).get("version", 0) or 0)):
                    return _empty_summary("deferred", "watch changed between planning and acquisition")
                watch = current
        else:
            active = sorted(active_watches(data, now), key=lambda w: str(w.get("id")))
            inferred = None
            if _inferred_enabled(policy) and (not active or inferred_turn(store, policy)):
                # Standalone path: explicit watches take precedence, but the
                # bounded durable fair rotation still offers periodic slots to
                # inferred discovery (never written to the watch store). With
                # zero explicit watches every slot is an inferred slot.
                inferred = prepare_inferred_plan(
                    policy, clock, store=store, planner=planner, context=context,
                    interest_proposer=interest_proposer, now=now, force=force_plan,
                    overall_budget=overall_budget, privacy_verifier=privacy_verifier)
            if inferred is not None:
                prepared = inferred
                plan = dict(prepared.get("plan") or {})
                used_model_any = bool(prepared.get("used_model"))
                watch_id = str(plan.get("watch_id") or "")
                from . import interestproposal as ip
                idata = ip.load_inferred_registry(store, strict=True)
                current = idata.get("goals", {}).get(watch_id)
                if (not isinstance(current, dict) or not ip.goal_is_active(current, now)
                        or int(current.get("version", 0) or 0)
                        != int((prepared.get("watch") or {}).get("version", 0) or 0)):
                    return _empty_summary("deferred", "inferred goal changed between planning and acquisition")
                if ip.goal_blocked_by_preference(current, policy, now):
                    return _empty_summary("deferred", "inferred goal blocked by preference")
                watch = ip.goal_to_watch(current)
                watch["plan"] = plan
                prepared = {**prepared, "origin": "inferred"}
            elif not active:
                return _empty_summary("no_active_watch", "no active watch")
            else:
                cursor = int(data.get("cursor", 0)) % len(active)
                watch = active[cursor]
                data["cursor"] = (cursor + 1) % len(active)
                effective_planner = planner
                plan_timeout: float | None = None
                if overall_budget is not None:
                    reserve = (float(policy.get("public_budget_seconds", 10))
                               + float(policy.get("obligation_budget_seconds", 15)))
                    fit = overall_budget.remaining() - reserve
                    if fit <= PLAN_DEFER_MIN_SECONDS:
                        effective_planner = None
                        planning_deferred = True
                    else:
                        plan_timeout = min(float(policy.get("adaptive_plan_timeout_seconds", 60)), fit)
                plan, used_model = plan_for_watch(watch, policy, effective_planner, now=now,
                                                  force=force_plan, plan_timeout=plan_timeout)
                used_model_any = bool(used_model)

        inferred_goal = str((prepared or {}).get("origin") or "") == "inferred"
        scope = str(plan.get("scope") or "public")
        watch_id = str(watch.get("id") or "")

        registry = load_adaptive_registry(store)
        sources_out: list[dict[str, Any]] = []
        raw_results: list[SourceResult] = []
        planned_desc = {
            "watch_id": watch_id,
            "watch_version": int(watch.get("version", 0) or 0),
            "plan_version": int(plan.get("watch_version", 0) or 0),
            "scope": scope,
            "semantic": bool(plan.get("semantic", False)),
            "provenance": str(plan.get("provenance") or ""),
            "queries": list(plan.get("queries") or []),
            "planning_deferred": planning_deferred,
            "origin": "inferred" if inferred_goal else "explicit",
        }

        unsupported_reason = str(plan.get("unsupported_reason") or "")
        # A plan with NO query is never successful coverage, even when the scope
        # is nominally supported: persist an explicit unsupported/skipped status
        # instead of a healthy ``ok`` with an empty count.
        if not is_supported_scope(scope) or not plan.get("queries"):
            reason = unsupported_reason or (
                f"scope {scope!r} has no supported public route"
                if not is_supported_scope(scope) else "plan produced no query")
            sources_out.append({
                "kind": "adaptive", "host": "", "interest": "",
                "status": "unsupported", "checked_at": "", "count": 0,
                "error": "", "reason": reason,
            })
            registry["updated_at"] = now.isoformat()
            save_adaptive_registry(store, registry)
            if not inferred_goal:
                save_watchstore(store, data)
            _record_coverage(coverage, now, watch_id, planned_desc, sources_out)
            store.save(ADAPTIVE_COVERAGE, coverage)
            if inferred_goal:
                from . import interestproposal as ip
                idata = ip.load_inferred_registry(store, strict=True)
                stored = idata.get("goals", {}).get(watch_id)
                if isinstance(stored, dict) and ip.goal_is_active(stored, now):
                    ip.note_attempt(stored, sources_out, policy, now)
                    ip.save_inferred_registry(store, idata)
            return {"status": "unsupported", "reason": reason,
                    "planned": [planned_desc], "sources": sources_out,
                    "planned_watches": 1, "used_model": used_model_any,
                    "pipeline_status": "completed",
                    "coverage_status": _coverage_status(sources_out),
                    "results": raw_results,
                    "origin": "inferred" if inferred_goal else "explicit"}

        ensure_adaptive_sources(registry, plan, policy, now)

        # Broad public discovery (opt-in, default OFF). Only run when a bridge is
        # injected AND policy enables it, so the default adaptive path is
        # byte-for-byte the existing GitHub/Reddit route. A healthy-empty,
        # unsupported or rate-limited backend registers NOTHING.
        discovery = None
        if search_bridge is not None and bool(policy.get("adaptive_broad_discovery_enabled", False)):
            from .discovery import discover_public_sources, ensure_discovered_sources
            from .searchbridge import NativeSearchBridge
            if isinstance(search_bridge, NativeSearchBridge):
                # Bind the safety/context revision too, so a changed privacy
                # decision/context can never replay a cached search result.
                search_bridge.bind_watch(
                    watch_id,
                    int(plan.get("watch_version") or watch.get("version") or 1),
                    str(plan.get("privacy_revision") or plan.get("context_revision") or ""))

            try:
                found, discovery = discover_public_sources(
                    plan, policy, search_bridge, budget=search_budget,
                    resolver=resolver, now=now, overall_budget=overall_budget)
            except Exception as exc:  # noqa: BLE001 - discovery never breaks the run
                found, discovery = [], {"status": "failed", "reason": "discovery error",
                                        "discovered": 0, "outcomes": [], "sources": []}
            ensure_discovered_sources(registry, plan, found, policy, now, resolver=resolver)
            planned_desc["discovery"] = discovery

        live = [rec for rec in registry["sources"].values()
                if str(rec.get("watch_id") or "") == watch_id]
        due = [rec for rec in live if _due(rec, now)]
        per_run = max(1, int(policy.get("adaptive_fetch_max_sources_per_run", 2)))
        if attempt_ledger is not None:
            # Draw from the SAME combined allowance as the ordinary feeds, so the
            # two paths can never add up to more than feed_max_sources_per_run.
            per_run = min(per_run, attempt_ledger.remaining())
        cursor_r = int(registry.get("feed_cursor", 0)) % max(1, len(due)) if due else 0
        ordered = due[cursor_r:] + due[:cursor_r]
        attempt = ordered[:per_run]
        deferred = ordered[per_run:]
        if attempt_ledger is not None:
            # Only actual attempts consume the allowance.
            attempt_ledger.take(len(attempt))
        registry["feed_cursor"] = (cursor_r + len(attempt)) % max(1, len(due)) if due else 0

        # The HTTP budget is created/started only NOW, after planning. When the
        # caller shares an existing public budget (main integration) it is used
        # as-is so adaptive work can never add a second additive cap.
        if budget is None:
            budget = Budget(float(policy.get("public_budget_seconds", 10)), label="adaptive")
        elif restart_budget:
            budget.restart()
        elif prepared is None:
            # Standalone: remove the (non-HTTP) planning phase from the shared
            # budget clock WITHOUT refilling any time already spent.
            budget.exclude(time.monotonic() - plan_started)

        reddit_budget = 1
        for rec in attempt:
            if budget.expired():
                row = _coverage_row(rec, None, status="skipped",
                                    reason="collector budget exhausted")
                sources_out.append(row)
                raw_results.append(_raw_from_row(rec, row))
                continue
            rec_policy = dict(policy)
            rec_policy["feed_timeout_seconds"] = max(
                1, int(min(int(policy.get("feed_timeout_seconds", pf.DEFAULT_TIMEOUT)),
                           budget.remaining())))
            try:
                result, used = pf._collect_one(rec, policy=rec_policy, clock=clock, now=now,
                                               fetcher=fetcher, reddit_budget=reddit_budget,
                                               reddit_ledger=reddit_ledger)
            except Exception as exc:  # noqa: BLE001 - one source never breaks the run
                result = SourceResult(f"feed:{rec.get('id','unknown')}", "error",
                                      error=pf.safe_source_error(exc, kind="feed"),
                                      checked_at=now.isoformat())
                used = 0
            if used:
                reddit_budget = 0
            sources_out.append(_coverage_row(rec, result, status=""))
            raw_results.append(result)
        for rec in deferred:
            row = _coverage_row(rec, None, status="skipped",
                                reason="deferred by per-run cap")
            sources_out.append(row)
            raw_results.append(_raw_from_row(rec, row))

        if inferred_goal:
            _tag_inferred_items(raw_results)

        retention_minutes = int(policy.get("public_findings_retention_days", 30)) * 24 * 60
        for rec in registry["sources"].values():
            if rec.get("cache_at") and pf._age_minutes(now, rec["cache_at"]) > retention_minutes:
                rec["cache_items"] = []
                rec["cache_at"] = ""
        # Persist/clear per-URL failure state AFTER the fetch phase so a failing
        # discovered source's backoff survives the reconciler dropping its slot,
        # and a healthy fetch clears it. Isolated adaptive registry only.
        try:
            from .discovery import note_discovered_failure_state
            note_discovered_failure_state(registry, now)
        except Exception:  # noqa: BLE001 - bookkeeping never breaks a run
            pass
        save_adaptive_registry(store, registry)
        if inferred_goal:
            # Bounded attempts/backoff on the inferred goal; never the watch store.
            from . import interestproposal as ip
            idata = ip.load_inferred_registry(store, strict=True)
            stored = idata.get("goals", {}).get(watch_id)
            if isinstance(stored, dict) and ip.goal_is_active(stored, now):
                ip.note_attempt(stored, sources_out, policy, now)
                ip.save_inferred_registry(store, idata)
        else:
            save_watchstore(store, data)
        _record_coverage(coverage, now, watch_id, planned_desc, sources_out)
        store.save(ADAPTIVE_COVERAGE, coverage)
        counts: dict[str, int] = {}
        for row in sources_out:
            counts[str(row.get("status"))] = counts.get(str(row.get("status")), 0) + 1
        return {"status": "ok", "reason": "", "planned": [planned_desc],
                "sources": sources_out, "planned_watches": 1,
                "used_model": used_model_any, "counts": counts,
                "pipeline_status": "completed",
                "coverage_status": _coverage_status(sources_out),
                "results": raw_results,
                "origin": "inferred" if inferred_goal else "explicit"}


def _record_coverage(
    coverage: dict[str, Any], now: datetime, watch_id: str,
    planned: dict[str, Any], sources: list[dict[str, Any]],
) -> None:
    entry = {
        "plan_version": planned.get("plan_version"),
        "scope": planned.get("scope"),
        "semantic": planned.get("semantic"),
        "provenance": planned.get("provenance"),
        "queries": planned.get("queries", []),
        "unsupported_reason": planned.get("unsupported_reason", ""),
        "last_attempt_at": now.isoformat(),
        "pipeline_status": "completed",
        "coverage_status": _coverage_status(sources),
        "sources": sources,
    }
    coverage.setdefault("watches", {})[watch_id] = entry
    keys = list(coverage["watches"].keys())
    if len(keys) > MAX_COVERAGE_WATCHES:
        for stale in keys[:-MAX_COVERAGE_WATCHES]:
            coverage["watches"].pop(stale, None)
    coverage["updated_at"] = now.isoformat()


def build_packet_results(results: list[SourceResult], policy: dict[str, Any]) -> list[SourceResult]:
    """Gate adaptive evidence for the ordinary feed packet.

    Enabled only by the explicit feature mode (``adaptive_mode == "active"``) or
    the legacy ``adaptive_evidence_enabled`` flag, so the non-adaptive baseline is
    byte-for-byte unchanged while disabled. Returns the approved ``SourceResult``
    objects so the ordinary ``build_packet`` feed gates process them exactly like
    any other feed -- there is no second packet path or additive cap.
    """
    mode = str(policy.get("adaptive_mode") or "disabled")
    enabled = mode == "active" or bool(policy.get("adaptive_evidence_enabled", False))
    if not enabled:
        return []
    return [r for r in results if isinstance(r, SourceResult)]
