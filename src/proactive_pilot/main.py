"""Command-line bridge for the proactive pilot candidate.

Commands (safe by construction; no arbitrary command execution):
  --collect-only   print redacted status (counts/health); never delivers
  --check          same as collect-only plus gate simulation; never delivers
  --dry-run        run evaluation but never print a deliverable message
  --run            default: emit a short final permitted alert OR nothing
  --self-test      run all deterministic tests with seeded clock/fixtures
  --canary         run the real native evaluator against a harmless fixture

Nothing is sent by this program. The native scheduler owns delivery: this
process only writes a final line to stdout (or stays silent).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import __version__
from .candidates import build_packet, packet_digest
from .clock import Clock, SeededClock
from .collectors import default_sources
from .contextsnapshot import inspect_metadata as snapshot_metadata
from .contextsnapshot import load_context_snapshot
from .evaluator import EvaluatorResult, FakeEvaluator, NativeEvaluator, fixture_packet, resolve_toolset_tools, validate_object
from .feedback import (
    FeedbackError,
    apply_action,
    find_records,
    load_preferences_file,
    receipt_for_action,
    save_preferences_file,
)
from .gates import (
    commit_coverage_notice,
    decide,
    deferred_coverage_notice,
    needs_evaluator_notice,
    note_evaluator_outcome,
    notice_allowed,
    record_delivery,
)
from .policy import in_quiet_hours, load_policy, load_preferences
from .state import CorruptStateError, StateStore
from .watchdog import RunDeadlineExceeded, run_deadline

CANDIDATE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_DIR = CANDIDATE_ROOT / "var"
DEFAULT_POLICY = CANDIDATE_ROOT / "policy" / "policy.json"
DEFAULT_PREFS = CANDIDATE_ROOT / "policy" / "preferences.json"


def _emit(text: str) -> None:
    """Write the native-scheduler stdout line.

    The native scheduler consumes every non-empty stdout line as a real
    user-facing message, so the production path must only ever write a genuine
    permitted message here (or stay silent). Diagnostics go to stderr.
    """
    if text:
        sys.stdout.write(text.strip() + "\n")


def _diag(payload: dict) -> None:
    """Explicit diagnostic for check/dry-run/lock/unchanged/paused skips.

    Always stderr, never stdout: a scheduler must not receive a diagnostic as a
    deliverable message. This is the explicit, reviewable surface for skips.
    """
    sys.stderr.write(json.dumps(payload, indent=2) + "\n")


def _interest_context(results: list) -> list[str]:
    """Bounded, non-private context for public discovery.

    Only the user's OWN task titles and calendar titles are used (never raw mail
    subjects or bodies). These titles are ephemeral DATA only: the inferred-goal
    privacy boundary (structural always-on plus an optional semantic review)
    screens every proposed concept before any public search query. Raw titles are
    never persisted, and no closed topic vocabulary is restored.
    """
    context: list[str] = []
    for res in results:
        kind = str(getattr(res, "name", "")).split(":", 1)[0]
        if kind not in {"tasks", "calendar"}:
            continue
        for item in getattr(res, "items", []):
            text = str(item.get("title") or item.get("subject") or "").strip()
            if text:
                context.append(text[:160])
            if len(context) >= 20:
                return context
    return context


def _adaptive_enabled(policy: dict) -> bool:
    return (str(policy.get("adaptive_mode") or "disabled") == "active"
            or bool(policy.get("adaptive_evidence_enabled", False)))


def _inferred_goals_enabled(policy: dict) -> bool:
    """True when the autonomous inferred-goals stage should run.

    It shares the adaptive acquisition path, so it counts as adaptive work: the
    default-off flag alone is enough when an explicit feature mode is not set.
    """
    return bool(policy.get("adaptive_inferred_goals_enabled", False))


def _adaptive_work_enabled(policy: dict) -> bool:
    return _adaptive_enabled(policy) or _inferred_goals_enabled(policy)


def _context_change_fingerprint(prefs: dict, assistant_entries: list[dict],
                                previous_mentions: list[dict]) -> str:
    """Keyed-eligibility fingerprint of the CONTEXT (not the evidence).

    A change to the assistant-mediated snapshot or to the user's preferences must
    re-EVALUATE the existing evidence within the normal cadence/gates, without
    fabricating urgency or resetting notification dedup. It is stored separately
    from the evidence digest so identical evidence + identical context stays a
    no-op, while identical evidence + changed context wakes the evaluator.
    """
    normalized = [
        {
            "source": str(e.get("source") or "")[:64],
            "kind": str(e.get("kind") or "")[:32],
            "advisory": bool(e.get("advisory", True)),
            "expires_at": str(e.get("expires_at") or "")[:64],
            "text": str(e.get("text") or "")[:240],
        }
        for e in list(assistant_entries or [])
    ]
    body = json.dumps({
        "preferences": prefs,
        "assistant_context": normalized,
        "previous_mentions": previous_mentions,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def collect_public_and_adaptive(
    policy: dict,
    clock,
    *,
    store,
    context: list[str] | None,
    public_budget,
    planner=None,
    fetcher=None,
    public_fetcher=None,
    overall_budget=None,
    paused: bool = False,
    adaptive_first: bool | None = None,
    search_bridge=None,
    search_budget=None,
    resolver=None,
    interest_proposer=None,
    privacy_verifier=None,
    native_ctx=None,
) -> list:
    """Collect the public category: ordinary feeds + (when enabled) adaptive.

    Both paths draw from the SAME ``public_budget`` and the SAME source-attempt
    ledger, so adaptive evidence can never add a second additive HTTP/source cap
    (combined attempts never exceed ``feed_max_sources_per_run``). The plan/cache
    refresh runs FIRST -- before any HTTP request and under the same watch lock
    and pause -- and its (non-HTTP) duration is removed from the shared HTTP
    clock without refilling anything ordinary feeds already spent. Adaptive is
    gated by the explicit feature mode; while disabled (the default) the ordinary
    baseline is byte-for-byte unchanged. Adaptive results are returned as
    ordinary ``SourceResult`` objects and pass through the ordinary feed packet
    gates in ``build_packet``.
    """
    from .adaptive import PublicAttemptLedger, build_packet_results, collect_adaptive
    from .adaptive import next_public_rotation, prepare_adaptive_plan
    from .planner import NativePlanner
    from .publicfeeds import RedditLedger, collect_public_feeds

    results: list = []
    feeds_enabled = bool(policy.get("feeds_enabled", True))
    adaptive_on = _adaptive_work_enabled(policy)
    inferred_on = _inferred_goals_enabled(policy)

    prepared = None
    if adaptive_on:
        if planner is None:
            # Native zero-tool planner is the DEFAULT when the feature is enabled;
            # tests inject a fake planner explicitly. When the host-built context
            # is available it uses the bounded ctx.llm lane; the subprocess route
            # remains the explicit compatibility fallback.
            planner = NativePlanner(ctx=native_ctx)
        if inferred_on and interest_proposer is None:
            # Native zero-tool PROPOSER is the default when inferred goals are on,
            # so enabling the flag never silently does zero goal selection.
            from .interestproposal import NativeInterestProposer
            interest_proposer = NativeInterestProposer(ctx=native_ctx)
        if (inferred_on and privacy_verifier is None
                and bool(policy.get("adaptive_privacy_review_enabled", False))):
            # Opt-in separate zero-tool SEMANTIC privacy review. Default OFF, so
            # enabling inferred goals adds zero extra model calls unless set. The
            # always-on STRUCTURAL privacy boundary runs regardless.
            from .interestproposal import NativePrivacyVerifier
            privacy_verifier = NativePrivacyVerifier(ctx=native_ctx)
        if adaptive_first is None:
            adaptive_first = next_public_rotation(store)
        prepared = prepare_adaptive_plan(policy, clock, store=store, planner=planner,
                                         context=context, interest_proposer=interest_proposer,
                                         paused=paused, overall_budget=overall_budget,
                                         privacy_verifier=privacy_verifier)
        elapsed = float(prepared.get("plan_elapsed") or 0.0)
        if elapsed > 0:
            public_budget.exclude(elapsed)
        # H1: when broad discovery is enabled and no bridge was injected (tests),
        # construct the REAL native adapter here -- otherwise enabling the flag
        # would silently do zero broad search. The persisted cache is keyed to the
        # prepared watch revision so an unchanged watch makes no repeat calls.
        if (search_bridge is None
                and bool(policy.get("adaptive_broad_discovery_enabled", False))):
            from .searchbridge import build_native_bridge
            _plan = (prepared or {}).get("plan") or {}
            search_bridge = build_native_bridge(
                policy, store=store,
                watch_id=str(_plan.get("watch_id") or ""),
                watch_version=int(_plan.get("watch_version") or 0),
                revision_token=str(_plan.get("privacy_revision")
                                   or _plan.get("context_revision") or ""))
    if adaptive_first is None:
        adaptive_first = False

    ledger = PublicAttemptLedger(int(policy.get("feed_max_sources_per_run", 2)),
                                 adaptive_first=bool(adaptive_first))
    # ONE shared per-cycle Reddit allowance across ordinary feeds AND adaptive
    # evidence, so the combined cycle never issues two Reddit requests.
    reddit_ledger = RedditLedger(1)

    def _run_adaptive() -> list:
        summary = collect_adaptive(policy, clock, store=store, planner=planner,
                                   fetcher=fetcher, budget=public_budget,
                                   attempt_ledger=ledger, prepared=prepared,
                                   search_bridge=search_bridge, search_budget=search_budget,
                                   resolver=resolver, reddit_ledger=reddit_ledger,
                                   interest_proposer=interest_proposer, context=context,
                                   privacy_verifier=privacy_verifier)
        return build_packet_results(summary.get("results", []), policy)

    def _run_ordinary() -> list:
        return collect_public_feeds(policy, clock, store=store, context=context,
                                    budget=public_budget, fetcher=public_fetcher,
                                    attempt_ledger=ledger, reddit_ledger=reddit_ledger)

    if adaptive_on and adaptive_first:
        results.extend(_run_adaptive())
    if feeds_enabled:
        results.extend(_run_ordinary())
    if adaptive_on and not adaptive_first:
        results.extend(_run_adaptive())
    return results


# Backwards-compatible name for the public-category collection hook.
collect_sources = collect_public_and_adaptive


def run_once(args: argparse.Namespace, *, evaluator=None, location_http=None) -> int:
    """Entry point used by the scheduler and tests.

    Arms a whole-run wall-clock bound (policy ``run_wallclock_seconds``) around
    the entire run so a spin/hang in collection, evaluation or state writing can
    never exceed it while holding the shared lock. The bound is a no-op off the
    main thread or without SIGALRM, where the per-call budgets still apply.
    """
    try:
        run_seconds = float(load_policy(getattr(args, "policy", None)).get("run_wallclock_seconds", 0) or 0)
    except Exception:  # noqa: BLE001 - a policy problem must not disable the run
        run_seconds = 0.0
    with run_deadline(run_seconds):
        return _run_once(args, evaluator=evaluator, location_http=location_http)


def _run_once(args: argparse.Namespace, *, evaluator=None, location_http=None) -> int:
    if not getattr(args, "command", None):
        args.command = "run"
    clock = SeededClock(args.seeded_clock) if args.seeded_clock else Clock()
    policy = load_policy(args.policy)
    # Optional validated per-user configuration lives beside the policy file.
    # A present-but-invalid configuration fails CLOSED (no collection, no
    # delivery) instead of silently running on defaults.
    try:
        from .userconfig import ConfigError, apply_user_config, default_config, load_user_config
        _uc_path = Path(args.policy).with_name("user_config.json") if args.policy else None
        _loaded = load_user_config(_uc_path, native=getattr(args, "native_ctx", None))
        if _loaded.present:
            policy = apply_user_config(policy, _loaded.config)
            if _loaded.warnings:
                for _warning in _loaded.warnings:
                    print(f"proactive-pilot: config warning: {_warning}", file=sys.stderr)
        elif getattr(args, "require_config", False):
            # Production entry with no configuration (fresh install, or config
            # deleted after setup): apply the NEUTRAL generic defaults so the run
            # never reports the old policy and then reads legacy private sources.
            # Exact-time reminders are unaffected; only explicit configuration
            # re-enables any private source.
            policy = apply_user_config(policy, default_config())
            print("proactive-pilot: no user configuration present; applying neutral "
                  "generic defaults (run `hermes secretary configure init|migrate`)",
                  file=sys.stderr)
    except ConfigError as exc:
        print(f"proactive-pilot: user configuration invalid, failing closed: {exc}",
              file=sys.stderr)
        _emit("")
        return 1
    try:
        prefs = load_preferences(args.preferences, strict=True)
    except FeedbackError as exc:
        print(f"proactive-pilot: preferences corrupt, failing closed: {exc}", file=sys.stderr)
        _emit("")
        return 1
    policy["_preferences"] = prefs
    # A global pause is absolute: it must not read any source, not even for a
    # --check/--collect-only diagnostic. Only a separate explicit override could
    # relax this, and no such override is designed or accepted here.
    paused = prefs.get("paused") or {}
    if paused.get("active"):
        from datetime import datetime

        until = paused.get("until")
        try:
            pause_active = not until or datetime.fromisoformat(until) > clock.now()
        except (ValueError, TypeError):
            pause_active = True
        if pause_active:
            _diag({
                "command": args.command,
                "diagnostic": "skip-paused",
                "reason": "global pause active; no source read",
            })
            _emit("")
            return 0
    # Diagnostics are allowed only for explicit check/dry-run, and only on
    # stderr; the production --run path (including lock/unchanged skips) is
    # silent-but-empty.
    emit_skip_diagnostics = bool(args.check or args.dry_run)
    store = StateStore(Path(args.state_dir), clock)

    with store.lock() as acquired:
        if not acquired:
            if emit_skip_diagnostics:
                _diag({
                    "command": args.command,
                    "diagnostic": "skip-lock",
                    "reason": "another run holds the lock",
                })
            _emit("")
            return 0  # overlapping run: no double work; diagnostic only

        try:
            state = store.load_notify_state(strict=True)
        except CorruptStateError as exc:
            print(f"proactive-pilot: state corrupt, failing closed: {exc}", file=sys.stderr)
            _emit("")
            return 1

        results = []
        # Budget handle for the opt-in local phone-context pass. In a real run it
        # is the reserved CORE allocation, so location work can never steal the
        # public/obligation reserved slots; in fixture mode it is a fresh ceiling.
        location_budget = None
        private_context = None
        # Bounded assistant-mediated context snapshot. It is a delivery cache the
        # assistant maintains explicitly from already-authorised context -- never
        # an automatic memory scrape. A no-model run is a pure offline check and
        # reads no snapshot, model or network. Missing/empty/malformed/stale
        # yields no entries (fail closed); nothing is fabricated or refreshed.
        snapshot_view = None
        assistant_entries: list[dict] = []
        assistant_texts: list[str] = []
        if not getattr(args, "no_model", False):
            snapshot_view = load_context_snapshot(store, clock.now())
            assistant_entries = list(snapshot_view.entries)
            assistant_texts = snapshot_view.texts()

        def _collect_private_context() -> dict | None:
            """Conditional local phone context; ZERO reads when not warranted.

            Global pause/quiet/no-eligible-event/cadence/insufficient-budget all
            yield a coordinate-free status (or None when mode is disabled).
            """
            if not policy.get("location_context_enabled", False):
                return None
            from .location import MIN_BUDGET_SECONDS, collect_location_context
            remaining = location_budget.remaining() if location_budget is not None else float(
                policy.get("collector_budget_seconds", 60))
            if remaining < MIN_BUDGET_SECONDS:
                return {"source": "phone_location", "status": "budget deferred",
                        "available": False, "fresh": False, "ephemeral": True}
            ctx = collect_location_context(
                results, clock=clock, policy=policy, prefs=prefs, state=state,
                http_get=location_http, deadline_seconds=remaining)
            return ctx.to_packet()

        if args.fixture:
            from .fixtures import fixture_results
            results = fixture_results(args.fixture, clock)
            from .collectors import Budget as _FixtureBudget
            location_budget = _FixtureBudget(
                float(policy.get("collector_budget_seconds", 60)), label="location-fixture")
            # A no-model fixture diagnostic is a pure offline check: it must never
            # read a private connector or the local location context.
            if not getattr(args, "no_model", False):
                private_context = _collect_private_context()
            context = _interest_context(results)
        else:
            from .collectors import Budget, SourceResult, safe_source_error
            # Fair category budgets over one overall 60 s wallclock. Each
            # category has a reserved allocation so a slow core mail pass cannot
            # consume the obligation slot, while the global cap still bounds the
            # sum. Allocations come from policy (core 35 / public 10 / obligation
            # 15 by default).
            overall = Budget(float(policy.get("collector_budget_seconds", 60)), label="overall")
            core_budget = Budget(float(policy.get("core_budget_seconds", 35)),
                                 parent=overall, label="core")
            for factory in default_sources(policy, clock, budget=core_budget):
                try:
                    results.append(factory())
                except Exception as exc:  # noqa: BLE001
                    results.append(SourceResult("unknown", "error",
                                                error=safe_source_error(exc, kind="source"),
                                                checked_at=clock.now().isoformat()))
            location_budget = core_budget
            # Location runs immediately after calendar collection (before any
            # expansion pass) so its time is drawn from the core allocation.
            private_context = _collect_private_context()
            # Awareness expansion runs on its reserved category allocation only.
            # The SAME accepted context (task/calendar titles + the bounded
            # assistant-mediated snapshot) reaches the inferred-interest proposer
            # and its privacy boundary; the snapshot is screened exactly like the
            # titles so a copied identifier can never become a public search.
            context = _interest_context(results)
            # Explicitly source-separated merge: task/calendar titles keep their
            # legacy proposer bound, accepted snapshot entries keep their own
            # 12/500 bound, and the proposer renders snapshot-first with reserved
            # task slots so a full calendar can never starve accepted memory.
            from .interestproposal import merge_interest_context
            proposer_context = merge_interest_context(context, assistant_texts)
            if policy.get("feeds_enabled", True) or _adaptive_work_enabled(policy):
                # ONE public HTTP/source budget shared by ordinary feeds and the
                # optional adaptive evidence path: no second additive cap. The
                # adaptive plan/cache refresh is prepared before the first public
                # HTTP request and clamped to the remaining overall collector
                # budget (see collect_public_and_adaptive / prepare_adaptive_plan).
                public_budget = Budget(float(policy.get("public_budget_seconds", 10)),
                                       parent=overall, label="public")
                try:
                    results.extend(collect_public_and_adaptive(
                        policy, clock, store=store, context=proposer_context,
                        public_budget=public_budget, overall_budget=overall,
                        planner=getattr(args, "adaptive_planner", None),
                        fetcher=getattr(args, "adaptive_fetcher", None),
                        public_fetcher=getattr(args, "public_fetcher", None),
                        search_bridge=getattr(args, "search_bridge", None),
                        search_budget=getattr(args, "search_budget", None),
                        resolver=getattr(args, "dns_resolver", None),
                        interest_proposer=getattr(args, "interest_proposer", None),
                        privacy_verifier=getattr(args, "privacy_verifier", None),
                        native_ctx=getattr(args, "native_ctx", None),
                    ))
                except Exception as exc:  # noqa: BLE001
                    results.append(SourceResult("feed:error", "error",
                                                error=safe_source_error(exc, kind="feed"),
                                                checked_at=clock.now().isoformat()))
            if policy.get("obligations_enabled", True):
                obligation_budget = Budget(float(policy.get("obligation_budget_seconds", 15)),
                                           parent=overall, label="obligation")
                try:
                    from .obligations import collect_obligations
                    results.extend(collect_obligations(policy, clock, store=store,
                                                       budget=obligation_budget))
                except Exception as exc:  # noqa: BLE001
                    results.append(SourceResult("obligation:error", "error",
                                                error=safe_source_error(exc, kind="obligation"),
                                                checked_at=clock.now().isoformat()))

        packet = build_packet(results, clock=clock, policy=policy, baseline=state,
                              context=context, private_context=private_context,
                              assistant_context=assistant_entries)
        state["health"] = dict(packet["coverage"])
        if snapshot_view is not None:
            # Bounded, text-free health for the delivery cache: available /
            # expired / invalid / unconfigured is visible without exposing text.
            coverage_health = snapshot_view.metadata()
            packet["context_snapshot"] = coverage_health
            state["health"]["context_snapshot"] = coverage_health
        packet["previous_mentions"] = [
            {"key": row.get("key"), "kind": row.get("kind"), "at": row.get("first_seen")}
            for row in list(state.get("items", {}).values())[-20:]
            if row.get("emitted") or row.get("delivered")
        ]
        # Evidence digest governs whether the MATERIAL changed; the separate
        # context/preference fingerprint governs re-evaluation eligibility. Both
        # must be unchanged for a skip, but delivery dedup still keys on the
        # candidate signature (a context-only change can never cause a repeat).
        digest = packet_digest(packet)
        context_digest = _context_change_fingerprint(
            prefs, assistant_entries, packet["previous_mentions"])
        # Real context is present when the accepted snapshot is nonempty OR the
        # user has real preferences. Only used to un-stick a LEGACY state that
        # predates the context fingerprint (see should_skip_inference).
        context_present = bool(assistant_entries) or _preferences_have_context(prefs)

        if args.collect_only or args.check:
            from .obligations import health, load_obligations
            from .publicfeeds import load_registry
            _diag({
                "command": args.command,
                "generated_at": packet["generated_at"],
                "coverage": packet["coverage"],
                "context_snapshot": packet.get("context_snapshot", {}),
                "candidate_count": len(packet["candidates"]),
                "digest": packet_digest(packet),
                "expansion": {
                    "registry_sources": len((load_registry(store).get("sources") or {})),
                    "obligations": health(load_obligations(store)),
                },
            })
            _emit("")
            return 0

        if args.dry_run:
            # Dry-run evaluates but never emits a deliverable message. The
            # proposal diagnostic goes to stderr so stdout stays empty.
            ev, route = select_evaluator(args, policy, evaluator)
            proposal = ev.evaluate(packet, policy)
            _diag({
                "command": args.command,
                "diagnostic": "dry-run",
                "no_emission": True,
                "proposal_action": proposal.action,
                "urgency": proposal.urgency,
                "reason": proposal.reason or proposal.error,
                "tools": proposal.tools,
                "latency_ms": proposal.latency_ms,
                "route": proposal.route or route,
            })
            _emit("")
            return 0

        if should_skip_inference(state, digest, clock.now(), policy,
                                 context_fingerprint=context_digest,
                                 context_present=context_present):
            # Upgrade a LEGACY record that predates the context fingerprint and
            # was skipped only because there is no real context: persist the key
            # so the next run compares it instead of re-checking forever. When
            # real context is present we never reach here (it re-evaluates once).
            last = state.get("last_inference")
            if (isinstance(last, dict) and last.get("context") is None
                    and context_digest):
                last["context"] = context_digest
            store.save("notify_state.json", state)
            if emit_skip_diagnostics:
                _diag({
                    "command": args.command,
                    "diagnostic": "skip-unchanged",
                    "reason": "packet digest unchanged and no quiet-hours wake is due",
                })
            _emit("")
            return 0

        if args.fixture:
            ev = evaluator or _fixture_evaluator(args.fixture)
        else:
            ev, _route = select_evaluator(args, policy, evaluator)

        proposal = ev.evaluate(packet, policy)
        quiet_now = in_quiet_hours(policy, clock.now())

        if args.canary:
            _emit(json.dumps({
                "command": args.command,
                "proposal_action": proposal.action,
                "urgency": proposal.urgency,
                "reason": proposal.reason or proposal.error,
                "tools": proposal.tools,
                "latency_ms": proposal.latency_ms,
            }, indent=2))
            return 0

        # Health notices are not an escape hatch: they obey the shared quiet /
        # pause / snooze / cooldown / cap guards, and the failure-signature is
        # only consumed once a notice is actually emitted.
        allowed, _guard_reason = notice_allowed(state, policy, prefs, clock.now())
        notice = deferred_coverage_notice(results, state, policy, clock.now(), commit=False)
        business_message = bool(proposal.message)

        # A higher-priority coverage notice is emitted first; the evaluator
        # failure notice is NOT consumed when coverage is emitted, so it stays
        # armed for the next run (only one health message per run).
        if notice and not business_message and allowed:
            rich_evaluator = note_evaluator_outcome(proposal, state, policy, prefs, clock.now())
            commit_coverage_notice(results, state, now=clock.now())
            state["last_delivery_at"] = clock.now().isoformat()
            state.setdefault("delivered", []).append({"at": clock.now().isoformat(), "signature": "coverage", "urgent": False})
            store.save("notify_state.json", state)
            _emit(notice)
            return 0

        evaluator_notice = note_evaluator_outcome(proposal, state, policy, prefs, clock.now())
        if evaluator_notice and allowed:
            state["last_delivery_at"] = clock.now().isoformat()
            state.setdefault("delivered", []).append({"at": clock.now().isoformat(), "signature": "evaluator-failure", "urgent": False})
            store.save("notify_state.json", state)
            _emit(evaluator_notice)
            return 0

        decision = decide(proposal, packet, clock=clock, policy=policy, prefs=prefs, state=state,
                          requested_reminder=bool(args.requested_reminder))
        if decision.get("send"):
            decision.setdefault("kind", proposal.kind)
            decision.setdefault("key", proposal.key)
            decision.setdefault("expiry", proposal.expiry)
            state = record_delivery(state, decision, packet, clock, policy)
        state["last_inference"] = {
            "digest": digest,
            "context": context_digest,
            "at": clock.now().isoformat(),
            "quiet_suppressed": bool(quiet_now and proposal.message and not decision.get("send")),
            "action": proposal.action,
            "pending": proposal.action in {"error", "invalid"} or (
                bool(proposal.message) and not decision.get("send")
                and decision.get("reason") in {"cooldown", "daily cap", "snoozed"}
            ),
        }
        baseline_tasks = {}
        for cand in packet.get("candidates", []):
            if cand.get("kind") == "tasks":
                baseline_tasks[cand["ref"]] = {
                    "version": cand.get("version", ""),
                    "status": cand.get("status", ""),
                    "due_date": str(cand.get("due_date") or cand.get("date") or "")[:10],
                    "phase": cand.get("phase", ""),
                }
        if baseline_tasks:
            state["baseline_tasks"] = baseline_tasks
            state["baseline_task_refs"] = sorted(baseline_tasks)
        store.prune_notify_state(state, int(policy.get("state_retention_days", 30)))
        store.save("notify_state.json", state)
        _emit(decision.get("message", ""))
        return 0


def select_evaluator(args, policy: dict, injected=None):
    """Resolve the evaluator route, preferring the supported native ``ctx.llm``.

    Returns ``(evaluator, route_name)``. A host context is only ever the one
    injected by the registered CLI handler closure (``args.native_ctx``); it is
    never constructed here. When no host lane is available the existing native
    subprocess evaluator is used, and the route is reported explicitly.
    """
    if injected is not None:
        return injected, "injected"
    if getattr(args, "no_model", False):
        return FakeEvaluator(EvaluatorResult("silence")), "no-model"
    ctx = getattr(args, "native_ctx", None)
    if ctx is not None:
        from .evaluator import NativeLlmEvaluator
        llm_evaluator = NativeLlmEvaluator(ctx, policy)
        if llm_evaluator.route.available:
            return llm_evaluator, "ctx.llm"
    return NativeEvaluator(), "subprocess"


def _preferences_have_context(prefs: dict) -> bool:
    """True when preferences carry real user context (rules/snooze/pause).

    Used only to decide whether a LEGACY state (written before the context
    fingerprint existed) must re-evaluate once so a preference correction is not
    suppressed. A default empty preference set stays a genuine no-op.
    """
    if not isinstance(prefs, dict):
        return False
    paused = prefs.get("paused")
    if isinstance(paused, dict) and paused.get("active"):
        return True
    return any(prefs.get(key) for key in ("explicit", "inferred", "snoozes"))


def should_skip_inference(state: dict, digest: str, now, policy: dict,
                          context_fingerprint: str | None = None,
                          context_present: bool = False) -> bool:
    """True when the evidence AND context/preference fingerprints are unchanged.

    The evidence digest decides whether the material changed. A SEPARATE
    context/preference fingerprint decides whether an unchanged evidence packet
    still needs re-evaluation because the assistant-mediated snapshot or the
    user's preferences changed. Both must match to skip. Delivery dedup is
    unaffected: re-evaluation alone never re-sends an already-delivered item.

    Re-evaluates when the prior run produced a pending proposal (so a suppressed
    alert is retried) or when it was quiet-suppressed and quiet hours have now
    ended (deferred meaningful candidates are not discarded).

    LEGACY MIGRATION: a state written before the context fingerprint existed has
    no ``context`` key. Treating that as "evidence-only" would silently suppress
    the FIRST context/preference change after migration. So when the prior state
    has no stored context fingerprint AND real context is present (a nonempty
    accepted snapshot or a real preference), we re-evaluate ONCE. A default empty
    legacy state (``context_present`` False) still skips, and the caller persists
    the context key, so there is no repeated re-evaluation and no repeat alert.
    """
    last = state.get("last_inference") or {}
    if not last or last.get("digest") != digest:
        return False
    if context_fingerprint is not None:
        prior_context = last.get("context")
        if prior_context is None:
            if context_present:
                # Legacy record missing the fingerprint: re-evaluate once to pick
                # up the new snapshot/preference instead of suppressing it.
                return False
        elif str(prior_context) != str(context_fingerprint):
            return False
    if last.get("pending"):
        return False
    if last.get("quiet_suppressed") and not in_quiet_hours(policy, now):
        return False
    if last.get("at") and not in_quiet_hours(policy, now):
        from datetime import datetime
        try:
            if in_quiet_hours(policy, datetime.fromisoformat(last["at"])):
                return False
        except (ValueError, TypeError):
            return False
    return True


def _fixture_evaluator(name: str):
    from .fixtures import fixture_evaluator
    return fixture_evaluator(name)


def self_test(args: argparse.Namespace) -> int:
    from .selftest import run_self_test
    return run_self_test()


def feedback_command(args: argparse.Namespace) -> int:
    """Schema-validated preference update. Writes nothing on invalid input."""
    clock = SeededClock(args.seeded_clock) if args.seeded_clock else Clock()
    now = clock.now()
    path = Path(args.preferences)
    try:
        prefs = load_preferences_file(path, strict=True)
        if args.feedback == "list":
            print(json.dumps(prefs, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.feedback == "show":
            target = str(getattr(args, "id", "") or "")
            if not target:
                print("proactive-pilot feedback rejected: --id is required", file=sys.stderr)
                return 2
            matches = find_records(prefs, target)
            print(json.dumps({"feedback": "show", "id": target,
                              "found": len(matches), "records": matches},
                             ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if matches else 1
        if args.feedback == "noop":
            # A clear "thanks"/silence message is a NO-OP: nothing is written and
            # the preference version is not bumped. This is the documented
            # assistant-mediated acknowledgement path, never a silent capture.
            print(json.dumps({"feedback": "noop", "status": "no-op",
                              "reason": "acknowledgement only; no preference recorded"},
                             indent=2))
            return 0
        action = {
            "action": args.feedback,
            "kind": args.kind,
            "match": args.match,
            "scope": args.scope,
            "provenance": args.provenance,
            "until": args.until,
            "minutes": args.minutes,
            "expires_at": args.expires_at,
            "id": args.id,
            "confidence": args.confidence,
            "note": args.note,
            "conditional": args.conditional,
            "exception": args.exception,
            "suppress": not args.no_suppress,
        }
        updated = save_preferences_file(path, apply_action(prefs, action, now), now)
    except FeedbackError as exc:
        print(f"proactive-pilot feedback rejected: {exc}", file=sys.stderr)
        return 2
    # Read back the exact persisted record so the assistant can show the user a
    # truthful receipt and never claim a change that was not stored.
    receipt = receipt_for_action(prefs, updated, action, now)
    print(json.dumps({
        "feedback": args.feedback,
        "status": "saved",
        "version": updated.get("version"),
        "updated_at": updated.get("updated_at"),
        "receipt": receipt,
    }, indent=2))
    return 0


def canary(args: argparse.Namespace) -> int:
    """Real native evaluator canary against a harmless synthetic fixture.

    Offline-capable: the model only reasons over the packet, and the evaluator
    toolset resolves to zero tools. No source is read and no message is sent.
    """
    key = "evt:canary:synthetic-1"
    packet = fixture_packet(key)
    policy = load_policy(args.policy)
    policy["_preferences"] = {"explicit": [], "inferred": [], "snoozes": [], "paused": {"active": False}}
    cfg = policy.get("evaluator", {})
    resolved_ok, resolved_names, resolved_detail = resolve_toolset_tools(
        str(cfg.get("toolset", "context_engine")),
        timeout=int(cfg.get("resolve_timeout_seconds", 30)),
    )
    if not resolved_ok:
        print(json.dumps({"canary": True, "resolved_toolset_ok": False, "reason": resolved_detail}, indent=2))
        return 1
    ev = NativeEvaluator()
    result = ev.evaluate(packet, policy)
    print(json.dumps({
        "canary": True,
        "resolved_toolset_ok": True,
        "resolved_toolset": resolved_names,
        "resolved_via": resolved_detail,
        "action": result.action,
        "reason": result.reason or result.error,
        "tools": result.tools,
        "latency_ms": result.latency_ms,
        "raw_head": (result.raw or "")[:200],
        "validated": validate_object(json.loads(result.raw), packet).action if result.action == "notify" and result.raw else result.action,
    }, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="proactive-pilot", description="Read-only proactive secretary bridge (candidate)")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--preferences", default=str(DEFAULT_PREFS))
    parser.add_argument("--seeded-clock", default=None)
    parser.add_argument("--fixture", default=None)
    parser.add_argument("--requested-reminder", action="store_true")
    parser.add_argument("--no-model", action="store_true", help="deterministic silence evaluator (tests)")
    parser.add_argument("--feedback", choices=[
        "add-explicit", "add-inferred", "snooze", "pause", "resume", "forget", "list",
        "show", "noop",
        "watch-add", "watch-list", "watch-change", "watch-stop", "watch-snooze",
        "watch-forget", "watch-feedback", "inferred-list", "inferred-revoke",
        "context-list", "context-show", "context-update", "context-revoke",
    ], default=None)
    parser.add_argument("--watch-topic", default="")
    parser.add_argument("--include", default="")
    parser.add_argument("--exclude", default="")
    parser.add_argument("--intake", default=None,
                        help="trusted natural-language watch intake (explicit requests only)")
    parser.add_argument("--intake-file", default=None,
                        help="path to a file containing one natural-language watch request")
    parser.add_argument("--shadow", action="store_true",
                        help="run the isolated adaptive shadow (public sources only)")
    parser.add_argument("--force-shadow", action="store_true",
                        help="allow shadow even when adaptive_shadow_enabled is false")
    parser.add_argument("--shadow-fixture", default=None,
                        help="offline fixture path for shadow recall (workspace only)")
    parser.add_argument("--kind", default="")
    parser.add_argument("--match", default="")
    parser.add_argument("--scope", default="")
    parser.add_argument("--provenance", default="")
    parser.add_argument("--until", default="")
    parser.add_argument("--minutes", type=int, default=None)
    parser.add_argument("--expires-at", default="")
    parser.add_argument("--id", default="")
    parser.add_argument("--confidence", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--conditional", action="store_true")
    parser.add_argument("--exception", default="")
    parser.add_argument("--no-suppress", action="store_true")
    parser.add_argument("--owner-declared", action="store_true",
                        help="context-update: mark the entry owner-declared (not advisory)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--collect-only", action="store_true")
    group.add_argument("--check", action="store_true")
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--run", action="store_true")
    group.add_argument("--self-test", action="store_true")
    group.add_argument("--canary", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.command = "self-test" if args.self_test else ("canary" if args.canary else ("dry-run" if args.dry_run else ("check" if args.check else ("collect-only" if args.collect_only else "run"))))
    try:
        if getattr(args, "intake", None) or getattr(args, "intake_file", None):
            from .intake import intake_command
            return intake_command(args)
        if args.feedback:
            if str(args.feedback).startswith("watch-"):
                from .watchstore import watch_command
                return watch_command(args)
            if str(args.feedback).startswith("inferred-"):
                from .interestproposal import inferred_command
                return inferred_command(args)
            if str(args.feedback).startswith("context-"):
                from .contextsnapshot import context_command
                return context_command(args)
            return feedback_command(args)
        if args.self_test:
            return self_test(args)
        if args.shadow:
            from .shadow import shadow_command
            return shadow_command(args)
        if args.canary:
            return canary(args)
        if args.run or args.dry_run or args.check or args.collect_only:
            return run_once(args)
        return run_once(args)
    except RunDeadlineExceeded:
        # BaseException on purpose: the whole run hit its wall-clock bound. The
        # lock is already released by ``StateStore.lock``'s finally. Report a
        # real (non-zero) failure on stderr only; stdout stays empty so the
        # scheduler never emits a diagnostic as a user-facing alert.
        print("proactive-pilot: run exceeded wall-clock bound; aborted", file=sys.stderr)
        _emit("")
        return 1
    except Exception as exc:  # noqa: BLE001 - fail silent to stdout, log to stderr
        print(f"proactive-pilot internal error: {exc}", file=sys.stderr)
        _emit("")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
