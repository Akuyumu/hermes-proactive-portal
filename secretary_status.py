"""Shared, bounded, read-only status projection for CLI and chat surfaces.

This module owns the one safe shape used by ``hermes secretary status`` and
``/secretary status``. It reads only existing profile metadata: validated
configuration, the plugin's own job-ownership ledger/native cron listing, the
common model-health incident and the ready preparation queue.

It never performs a model/provider call, writes or rewrites a cron job, probes a
source or exposes prompts, private facts, titles, URLs, credentials or caller
argv. Every string is bounded and every list is capped.
"""
from __future__ import annotations

from typing import Any

MAX_LIST = 5
MAX_TEXT = 160
# Status is a chat/CLI projection, not a scheduler mutation. When a recorded job
# actually needs verifying, keep the native read short and bounded.
MAX_NATIVE_SECONDS = 8


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "").replace("\n", " ").replace("\r", " ").strip()[:limit]


def _bounded_list(values: Any, *, limit: int = MAX_LIST) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    out: list[str] = []
    for value in values:
        text = _text(value, 80)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _read_prefs() -> dict[str, Any]:
    try:
        from proactive_pilot.feedback import load_preferences_file
        from . import paths
        return load_preferences_file(paths.preferences_path(), strict=False)
    except Exception:  # noqa: BLE001 - status degrades, never crashes
        return {}


def _paused(prefs: dict[str, Any]) -> bool:
    paused = prefs.get("paused") or {}
    return bool(isinstance(paused, dict) and paused.get("active"))


def _config_summary(ctx: Any, service: Any = None,
                    info: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    """Return the validated config summary and a bounded error label."""
    try:
        if service is not None:
            info = info if isinstance(info, dict) else service.inspect()
            effective = info.get("effective_config") or {}
            summary = dict(effective.get("summary") or {})
            if effective.get("present"):
                summary["present"] = True
                state = info.get("state") or {}
                destination = (state.get("destination") or {}).get("target") or ""
                summary["destination"] = str(destination)
            return summary, ""
        from proactive_pilot.userconfig import load_user_config
        from . import paths
        loaded = load_user_config(paths.user_config_path(), native=ctx)
        return loaded.summary(), ""
    except Exception as exc:  # noqa: BLE001 - a broken config is reported, not guessed
        return {}, type(exc).__name__


def _route_summary(ctx: Any, service: Any = None,
                   info: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if service is not None:
            info = info if isinstance(info, dict) else service.inspect()
            route = dict(info.get("model_routes") or {})
        else:
            from proactive_pilot.native_runtime import bound_model_lane_status
            route = dict(bound_model_lane_status(ctx) or {})
            if not route:
                from .secretary_models import effective_route_status
                route = effective_route_status(ctx, strict_active=False)
    except Exception as exc:  # noqa: BLE001 - route truth degrades, never guessed
        route = {"strict_active": False, "blockers": [type(exc).__name__],
                 "configured_model_calls_allowed": False}
    primary = route.get("primary") if isinstance(route.get("primary"), dict) else {}
    fallback = route.get("fallback") if isinstance(route.get("fallback"), dict) else None
    return {
        "status": route,
        "primary": {
            "provider": _text(primary.get("provider"), 80),
            "model": _text(primary.get("model"), 80),
            "source": _text(primary.get("source"), 40),
        },
        "fallback": ({
            "provider": _text(fallback.get("provider"), 80),
            "model": _text(fallback.get("model"), 80),
            "source": _text(fallback.get("source"), 40),
        } if isinstance(fallback, dict) else None),
        "strict_active": bool(route.get("strict_active", False)),
        "strict_state": "active" if route.get("strict_active") else "unsupported",
        "configured_model_calls_allowed": bool(
            route.get("configured_model_calls_allowed", False)),
        "blockers": _bounded_list(route.get("blockers")),
    }


def _owned_jobs(backend: Any = None, *, probe_empty: bool = False) -> dict[str, Any]:
    """Read the ownership ledger, then verify native rows only when recorded.

    An empty ledger means this plugin has created no jobs, so there is nothing to
    verify unless the caller explicitly probes. When a ledger row does exist, the
    native read is bounded and its failure leaves the recorded-but-unverified
    state honest instead of inventing ownership.

    A backend that exposes the in-process ``list_jobs_for_status`` reader is
    preferred: status must never spawn another managed runtime (which can start a
    dependency installer under an empty profile). Injected protocol fakes keep
    the legacy ``list_jobs`` path.
    """
    recorded: list[dict[str, str]] = []
    try:
        from . import cron as cron_mod
        from . import paths
        ownership = cron_mod.load_job_ownership(paths.job_ownership_path())
        jobs = ownership.get("jobs") if isinstance(ownership, dict) else ownership
        rows = jobs.values() if isinstance(jobs, dict) else jobs or []
        for row in rows:
            if isinstance(row, dict):
                recorded.append({"id": _text(row.get("id"), 80),
                                 "name": _text(row.get("name"), 80)})
            elif isinstance(row, str):
                recorded.append({"id": "", "name": _text(row, 80)})
    except Exception:  # noqa: BLE001 - no ledger means none recorded
        recorded = []
    owned: list[dict[str, str]] = list(recorded)
    foreign: list[dict[str, Any]] = []
    missing: list[str] = []
    state = "owned" if owned else "none"
    next_run = ""
    available = False
    if not recorded and not probe_empty:
        # Chat/mobile status: no child CLI at all; the ledger is the whole answer.
        return {
            "available": False,
            "owned": [],
            "ids": [],
            "count": 0,
            "next_schedule": "",
            "state": "none",
            "foreign": [],
            "missing": [],
        }
    previous_timeout = None
    try:
        from . import cron as cron_mod
        from . import paths
        if backend is None:
            backend = cron_mod.HermesCronBackend(timeout=MAX_NATIVE_SECONDS)
        status_reader = getattr(backend, "list_jobs_for_status", None)
        if callable(status_reader):
            jobs = status_reader()
        else:
            if getattr(backend, "timeout", None) is not None:
                previous_timeout = backend.timeout
                backend.timeout = min(int(previous_timeout), MAX_NATIVE_SECONDS)
            jobs = backend.list_jobs()
        available = True
        ownership = cron_mod.load_job_ownership(paths.job_ownership_path())
        classified = cron_mod.classify_jobs(jobs, ownership)
        state = _text(classified.get("state"), 32) or state
        foreign = [{"id": _text(row.get("id"), 80),
                    "name": _text(row.get("name"), 80)}
                   for row in classified.get("foreign") or []
                   if isinstance(row, dict)][:MAX_LIST]
        missing = _bounded_list(classified.get("missing"))
        owned = [{"id": _text(row.get("id"), 80),
                  "name": _text(row.get("name"), 80),
                  "state": _text(row.get("state"), 32)}
                 for row in classified.get("owned") or []
                 if isinstance(row, dict)][:MAX_LIST]
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if any(_text(job.get("id"), 80) == row["id"] for row in owned):
                next_run = _text(job.get("next_run") or job.get("next"), 80)
                if next_run:
                    break
    except Exception:  # noqa: BLE001 - recorded but unverified is honest
        available = False
    finally:
        if previous_timeout is not None:
            try:
                backend.timeout = previous_timeout
            except Exception:  # noqa: BLE001 - injected backend may be immutable
                pass
    return {
        "available": available,
        "owned": owned[:MAX_LIST],
        "ids": _bounded_list([row.get("id") or row.get("name") for row in owned]),
        "count": len(owned),
        "next_schedule": next_run,
        "state": state,
        "foreign": foreign,
        "missing": missing,
    }


def _health_and_queue() -> tuple[dict[str, Any], dict[str, Any]]:
    health: dict[str, Any] = {"status": "unknown", "notice_status": ""}
    queue: dict[str, Any] = {"ready": 0, "ids": []}
    try:
        from proactive_pilot.clock import Clock
        from proactive_pilot.modelhealth import status as model_health_status
        from proactive_pilot.preparation import list_ready
        from proactive_pilot.state import StateStore
        from . import paths
        store = StateStore(paths.state_dir(), Clock())
        current = model_health_status(store)
        incident = current.get("incident")
        if isinstance(incident, dict):
            notice = current.get("notice")
            health = {
                "status": _text(incident.get("status"), 32) or "open",
                "notice_status": (_text(notice.get("status"), 32)
                                  if isinstance(notice, dict) else ""),
                "retry_after": _text(incident.get("retry_after"), 64),
            }
        else:
            health = {"status": "ok", "notice_status": ""}
        ready = list_ready(store)
        queue = {
            "ready": len(ready),
            "ids": _bounded_list([row.get("responsibility_id") for row in ready
                                  if isinstance(row, dict)]),
        }
    except Exception as exc:  # noqa: BLE001 - report degraded, never fake healthy
        health = {"status": "unknown", "notice_status": "", "error": type(exc).__name__}
        queue = {"ready": 0, "ids": [], "error": type(exc).__name__}
    return health, queue


def _onboarding(ctx: Any, service: Any = None,
                info: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        if service is not None:
            info = info if isinstance(info, dict) else service.inspect()
            state = info.get("state") or {}
            return {
                "status": _text(state.get("status"), 32) or "not_started",
                "next_step": _text(info.get("next_step"), 48),
                "next_question": _text(info.get("next_question"), 500),
                "destination_status": _text(
                    (state.get("destination") or {}).get("status"), 32),
                "confirmed_at": _text(
                    (state.get("destination") or {}).get("confirmed_at"), 64),
            }
        from .secretary_onboarding import next_step
        raw = None
        state_facade = getattr(ctx, "state", None)
        if state_facade is not None:
            raw = state_facade.get("onboarding_state")
        state = raw if isinstance(raw, dict) else {"status": "not_started"}
        return {
            "status": _text(state.get("status"), 32) or "not_started",
            "next_step": _text(next_step(state), 48),
            "next_question": "",
            "destination_status": _text(
                (state.get("destination") or {}).get("status"), 32),
            "confirmed_at": _text(
                (state.get("destination") or {}).get("confirmed_at"), 64),
        }
    except Exception as exc:  # noqa: BLE001 - no onboarding state is simply pending
        return {"status": "not_started", "next_step": "basics",
                "next_question": "", "destination_status": "unconfigured",
                "confirmed_at": "", "error": type(exc).__name__}


def collect(ctx: Any, service: Any = None, *,
            cron_backend: Any = None, probe_empty: bool = False) -> dict[str, Any]:
    """Build the bounded safe status shared by CLI and chat."""
    info: dict[str, Any] | None = None
    if service is not None:
        try:
            info = service.inspect()
        except Exception:  # noqa: BLE001 - the projection still degrades safely
            info = None
    config, config_error = _config_summary(ctx, service, info)
    route = _route_summary(ctx, service, info)
    jobs = _owned_jobs(cron_backend, probe_empty=probe_empty)
    health, queue = _health_and_queue()
    onboarding = _onboarding(ctx, service, info)
    prefs = _read_prefs()
    destination = _text(config.get("destination"), 80)
    return {
        "installed": True,
        "paused": _paused(prefs),
        "configured": bool(config.get("present")),
        "config_error": config_error,
        "timezone": _text(config.get("timezone"), 64),
        "delivery": {
            "configured": bool(destination),
            "state": ("pending-confirmation"
                      if onboarding.get("status") == "pending_verification"
                      else ("unverified" if destination else "not-configured")),
            "pending": bool(destination and onboarding.get("status") != "ready"),
        },
        "scheduled": bool(jobs.get("count") or jobs.get("state") == "owned"),
        "model_route": route,
        "model_status": route.get("status") or {},
        "strict_active": route.get("strict_active", False),
        "model_blockers": route.get("blockers") or [],
        "onboarding": onboarding,
        "jobs": {key: jobs.get(key) for key in
                 ("available", "state", "owned", "foreign", "missing")},
        "owned_jobs": jobs.get("owned") or [],
        "owned_job_ids": jobs.get("ids") or [],
        "owned_job_count": int(jobs.get("count") or 0),
        "next_schedule": jobs.get("next_schedule") or "",
        "health_incident": health,
        "prepare_queue": queue,
    }


def render_chat(status: dict[str, Any]) -> str:
    """Concise, bounded Telegram-safe rendering (a few lines, IDs only)."""
    route = status.get("model_route") or {}
    primary = route.get("primary") or {}
    route_label = " ".join(part for part in (
        _text(primary.get("provider"), 60), _text(primary.get("model"), 60)) if part)
    route_label = route_label or "inherited chat model"
    blockers = route.get("blockers") or []
    onboarding = status.get("onboarding") or {}
    delivery = status.get("delivery") or {}
    health = status.get("health_incident") or {}
    queue = status.get("prepare_queue") or {}
    job_ids = status.get("owned_job_ids") or []
    lines = [
        ("Secretary: installed; "
         + ("paused" if status.get("paused") else "active")
         + f"; setup={onboarding.get('status') or 'not_started'}"
         + f"; configured={'yes' if status.get('configured') else 'no'}"
         + f"; scheduled={'yes' if status.get('scheduled') else 'no'}"
         + f"; delivery={delivery.get('state') or 'not-configured'}"),
        (f"Model: {route_label}; strict={route.get('strict_state') or 'unsupported'}"
         + (f"; blockers={'; '.join(blockers[:3])}" if blockers else "")),
        (f"Jobs: {status.get('owned_job_count', 0)} owned"
         + (f" [{', '.join(job_ids[:5])}]" if job_ids else " (none recorded)")
         + f"; next={status.get('next_schedule') or 'none recorded'}"),
        (f"Health: {health.get('status') or 'unknown'}"
         + (f" (notice {health.get('notice_status')})"
            if health.get("notice_status") else "")
         + f"; prepare queue={int(queue.get('ready') or 0)} ready"
         + (f" [{', '.join((queue.get('ids') or [])[:5])}]"
            if queue.get("ids") else "")),
    ]
    if onboarding.get("status") == "pending_verification":
        action = (onboarding.get("next_question")
                  or "Run hermes secretary setup --deliver <saved-target> in the "
                     "foreground, then an authorised native canary.")
        lines.append(f"Next (delivery_verification): {_text(action, 400)}")
    elif onboarding.get("next_step"):
        lines.append(f"Next: {onboarding.get('next_step')}")
    return "\n".join(lines)
