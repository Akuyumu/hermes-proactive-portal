"""Production responsibility intake and lifecycle surfaces.

This module is the thin foreground seam over the validated
``proactive_pilot.responsibilities`` ledger. It exposes:

* ``hermes secretary responsibilities ...`` for an explicit foreground caller;
* one registered ``secretary_responsibility`` tool for the ordinary
  conversation surface; and
* ``responsibility_context``/``remember_human_turn`` for the registered
  ``pre_llm_call`` hook.

Authority rules:

* an accepted record requires a real current human turn (or an explicit
  foreground CLI invocation), never a model assertion alone;
* the tool must quote text that actually appears in the latest stored human
  turn for its session; the hook stores that turn itself, not the model;
* inferred opportunities are tentative and never become due work;
* thanks/silence/questions that are not commitments create nothing;
* duplicate capture of the same human turn is idempotent;
* handoff only inspects a current verified artifact and never executes it.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

try:  # package import (installed host) or top-level test import
    from . import paths
except ImportError:  # pragma: no cover - direct module execution
    import paths  # type: ignore[no-redef]

TURN_KEY = "responsibility_human_turn"
TURN_TTL_SECONDS = 30 * 60
MAX_TURNS = 24

_COMMITMENT = re.compile(
    r"\b(?:i will|i'll|i need to|i have to|i must|i should|i'm going to|"
    r"i am going to|remember to|please remind me|remind me to|please prepare|"
    r"please draft|can you prepare|could you prepare|we need to|let's|"
    r"please make sure|please follow up)\b", re.IGNORECASE)
_NOT_COMMITMENT = re.compile(
    r"^\s*(?:thanks|thank you|ok(?:ay)?|sure|noted|got it|cool|great)\s*[.!]*\s*$",
    re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, Mapping) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts)
    return "" if value is None else str(value)


def _clean(text: Any, limit: int = 4096) -> str:
    value = _text(text).replace("\r", " ").replace("\n", " ").strip()
    value = _CONTROL.sub("", value)
    return value[:limit]


def _facade(ctx: Any) -> Any:
    return getattr(ctx, "state", None) if ctx is not None else None


def _get(ctx: Any, key: str, default: Any = None) -> Any:
    facade = _facade(ctx)
    if facade is None:
        return default
    try:
        return facade.get(key, default)
    except Exception:  # noqa: BLE001
        return default


def _set(ctx: Any, key: str, value: Any) -> bool:
    facade = _facade(ctx)
    if facade is None:
        return False
    try:
        facade.set(key, value)
        return True
    except Exception:  # noqa: BLE001
        return False


def _store() -> Any:
    from proactive_pilot.clock import Clock
    from proactive_pilot.state import StateStore

    return StateStore(paths.state_dir(), Clock())


def _resp() -> Any:
    from proactive_pilot import responsibilities

    return responsibilities


def is_commitment(text: Any) -> bool:
    value = _clean(text, 2000)
    if not value or _NOT_COMMITMENT.match(value):
        return False
    return bool(_COMMITMENT.search(value))


def _turn_ref(session_id: str, text: str, at: float) -> str:
    digest = hashlib.sha256(
        f"{session_id}|{text}|{at}".encode("utf-8")).hexdigest()
    return "conversation:" + digest[:24]


def remember_human_turn(ctx: Any, *, session_id: Any, user_message: Any,
                        platform: Any = "", at: float | None = None) -> dict[str, Any] | None:
    """Store the actual eligible human turn for later exact-quote capture."""
    text = _clean(user_message)
    sid = _clean(session_id, 200)
    if not text or not sid:
        return None
    stamp = float(at if at is not None else time.time())
    record = {
        "session_id": sid,
        "text": text,
        "platform": _clean(platform, 40),
        "at": stamp,
        "ref": _turn_ref(sid, text, stamp),
    }
    previous = _get(ctx, TURN_KEY)
    rows = list(previous) if isinstance(previous, list) else []
    cutoff = stamp - TURN_TTL_SECONDS
    rows = [row for row in rows if isinstance(row, Mapping)
            and float(row.get("at") or 0) >= cutoff
            and _clean(row.get("session_id"), 200) == sid]
    rows.append(record)
    _set(ctx, TURN_KEY, rows[-MAX_TURNS:])
    return record


def latest_turn(ctx: Any, *, session_id: Any) -> dict[str, Any] | None:
    sid = _clean(session_id, 200)
    rows = _get(ctx, TURN_KEY)
    if not isinstance(rows, list):
        return None
    cutoff = time.time() - TURN_TTL_SECONDS
    for row in reversed(rows):
        if not isinstance(row, Mapping):
            continue
        if _clean(row.get("session_id"), 200) != sid:
            continue
        if float(row.get("at") or 0) < cutoff:
            return None
        return dict(row)
    return None


def _quote_matches(quote: Any, turn: Mapping[str, Any]) -> bool:
    evidence = _clean(quote, 2000)
    if not evidence:
        return False
    haystack = _clean(turn.get("text"), 4000)
    return evidence in haystack


def _provenance(turn: Mapping[str, Any]) -> dict[str, str]:
    return {
        "kind": "user_commitment",
        "source_ref": str(turn["ref"]),
        "conversation_ref": str(turn["ref"]),
        "at": _iso(float(turn["at"])),
    }


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _find_same_turn(records: list[dict[str, Any]], source_ref: str) -> dict[str, Any] | None:
    for rec in records:
        if str((rec.get("provenance") or {}).get("source_ref") or "") == source_ref:
            return rec
    return None


def capture(args: Mapping[str, Any], *, ctx: Any = None,
            now: Any = None) -> dict[str, Any]:
    """Capture one commitment or tentative opportunity from a real human turn."""
    R = _resp()
    action_kind = str(args.get("kind") or "commitment").strip().lower()
    if action_kind not in {"commitment", "opportunity"}:
        raise ValueError("kind must be commitment or opportunity")
    desired = _clean(args.get("desired_outcome"), 500)
    if not desired:
        raise ValueError("desired_outcome is required")
    criterion = _clean(args.get("acceptance_criterion"), 400) or (
        "The requested outcome is completed and read back in the foreground")
    session_id = _clean(args.get("session_id"), 200)
    evidence = args.get("evidence_quote")
    if ctx is not None:
        turn = latest_turn(ctx, session_id=session_id)
        if turn is None:
            raise ValueError("no current human conversation turn for this session")
        if not _quote_matches(evidence, turn):
            raise ValueError("evidence_quote is not present in the current human turn")
        if action_kind == "commitment" and not is_commitment(turn.get("text")):
            raise ValueError("the current human turn is not a clear commitment")
        provenance = _provenance(turn)
        accepted = action_kind == "commitment"
    else:
        source_ref = _clean(args.get("source_ref"), 240)
        conversation_ref = _clean(args.get("conversation_ref"), 240) or source_ref
        if not source_ref.startswith("conversation:"):
            raise ValueError("a foreground CLI create requires a conversation: source_ref")
        if not _clean(evidence, 2000):
            raise ValueError("evidence_quote is required")
        provenance = {"kind": "user_commitment" if action_kind == "commitment"
                      else "opportunity",
                      "source_ref": source_ref,
                      "conversation_ref": conversation_ref,
                      "at": _iso(time.time())}
        accepted = action_kind == "commitment"

    store = _store()
    existing = _find_same_turn(R.list_records(store), provenance["source_ref"])
    if existing is not None:
        return {"ok": True, "deduplicated": True, "responsibility": existing,
                "note": "This human turn was already captured; no duplicate was created."}
    rec = R.create(
        store,
        desired_outcome=desired,
        acceptance_criterion=criterion,
        provenance=provenance,
        status=R.ACCEPTED if accepted else R.TENTATIVE,
        due_at=args.get("due_at"),
        next_check_at=args.get("next_check_at"),
        source_version=args.get("source_version") or "",
        evidence_refs=args.get("evidence_refs") or [provenance["source_ref"]],
        facts=args.get("facts"),
        external_refs=args.get("external_refs"),
        now=now,
    )
    return {"ok": True, "deduplicated": False, "responsibility": rec,
            "note": ("Accepted from the current human commitment; no external action was taken."
                     if accepted else
                     "Recorded as a tentative opportunity only; it is not adopted work.")}


def _cli_confirmation_provenance(args: Mapping[str, Any], *, now: Any = None) -> dict[str, str]:
    """Opaque CLI-user authority without a forged conversation quote."""
    from datetime import datetime, timezone

    stamp = now if now is not None else datetime.now(timezone.utc)
    try:
        invocation = stamp.isoformat()
    except Exception:  # noqa: BLE001
        invocation = str(stamp)
    try:
        import paths as plugin_paths
        scope = str(plugin_paths.data_root())
    except Exception:  # noqa: BLE001 - still opaque without the profile path
        scope = "profile"
    evidence = _clean(args.get("evidence_quote"), 2000)
    raw = f"{scope}|{rid_token(args)}|{evidence}|{invocation}"
    ref = "cli-confirmation:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return {"kind": "user_commitment", "source_ref": ref,
            "conversation_ref": "", "at": invocation}


def rid_token(args: Mapping[str, Any]) -> str:
    return _clean(args.get("responsibility_id"), 96)


def _require_current_human(args: Mapping[str, Any], ctx: Any | None) -> None:
    if ctx is None:
        if not _clean(args.get("evidence_quote"), 2000):
            raise ValueError("foreground CLI lifecycle change requires evidence_quote")
        return
    turn = latest_turn(ctx, session_id=args.get("session_id"))
    if turn is None or not _quote_matches(args.get("evidence_quote"), turn):
        raise ValueError("lifecycle change requires the current human turn as evidence")


def dispatch(args: Mapping[str, Any], *, ctx: Any = None, now: Any = None) -> dict[str, Any]:
    R = _resp()
    action = str(args.get("action") or "list").strip().lower()
    store = _store()
    if action == "list":
        return {"ok": True, "responsibilities": R.list_records(store)}
    if action == "inspect":
        rid = _clean(args.get("responsibility_id"), 96)
        return {"ok": True, "responsibility": R.inspect(store, rid, include_claim=False)}
    if action in {"capture", "create"}:
        return capture(args, ctx=ctx, now=now)
    if action == "accept":
        _require_current_human(args, ctx)
        rid = _clean(args.get("responsibility_id"), 96)
        provenance = None
        if ctx is not None:
            turn = latest_turn(ctx, session_id=args.get("session_id"))
            provenance = _provenance(turn)
        else:
            # A registered/foreground CLI accept is itself an explicit human
            # confirmation. It must not reuse the tentative opportunity
            # provenance (which can never authorize acceptance), and it must not
            # forge a stored conversation quote. Bind an opaque, profile-scoped
            # invocation reference to this exact confirmation instead.
            provenance = _cli_confirmation_provenance(args, now=now)
        return {"ok": True, "responsibility": R.accept(
            store, rid, provenance=provenance, now=now)}
    if action == "update":
        _require_current_human(args, ctx)
        rid = _clean(args.get("responsibility_id"), 96)
        fields = {key: args.get(key) for key in (
            "desired_outcome", "acceptance_criterion", "due_at", "next_check_at",
            "source_version", "evidence_refs", "completion_evidence_refs", "facts",
            "external_refs") if key in args}
        if not fields:
            raise ValueError("update requires at least one changed field")
        return {"ok": True, "responsibility": R.update(store, rid, now=now, **fields)}
    if action == "cancel":
        _require_current_human(args, ctx)
        rid = _clean(args.get("responsibility_id"), 96)
        return {"ok": True, "responsibility": R.cancel(
            store, rid, reason=_clean(args.get("reason"), 400), now=now)}
    if action in {"handoff", "inspect_handoff"}:
        from proactive_pilot.preparation import foreground_handoff

        rid = _clean(args.get("responsibility_id"), 96)
        return {"ok": True, "handoff": foreground_handoff(store, rid, now=now),
                "execution": "not_performed"}
    raise ValueError(f"unknown responsibility action {action!r}")


def tool_handler(ctx: Any):
    def handler(args: Mapping[str, Any], **_kwargs: Any) -> str:
        try:
            return json.dumps(dispatch(args, ctx=ctx), ensure_ascii=False,
                              sort_keys=True)
        except Exception as exc:  # noqa: BLE001 - bounded tool error, no raw state
            return json.dumps({"ok": False,
                               "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
                              ensure_ascii=False, sort_keys=True)
    return handler


TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [
            "list", "inspect", "capture", "accept", "update", "cancel", "handoff"]},
        "responsibility_id": {"type": "string"},
        "session_id": {"type": "string"},
        "evidence_quote": {"type": "string"},
        "kind": {"type": "string", "enum": ["commitment", "opportunity"]},
        "desired_outcome": {"type": "string"},
        "acceptance_criterion": {"type": "string"},
        "due_at": {"type": "string"},
        "next_check_at": {"type": "string"},
        "source_version": {"type": "string"},
        "source_ref": {"type": "string"},
        "conversation_ref": {"type": "string"},
        "reason": {"type": "string"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "completion_evidence_refs": {"type": "array", "items": {"type": "string"}},
        "facts": {"type": "array", "items": {"type": "object"}},
        "external_refs": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["action"],
    "additionalProperties": False,
}


def register_tool(ctx: Any, *, register_tool: Any = None) -> dict[str, Any]:
    register = register_tool if register_tool is not None else getattr(ctx, "register_tool", None)
    if not callable(register):
        return {"responsibility_tool": False, "error": "register_tool unavailable"}
    try:
        register(
            "secretary_responsibility", "secretary", TOOL_SCHEMA, tool_handler(ctx),
            description=("Capture or manage an accepted responsibility from a real current "
                         "human commitment. Tentative opportunities stay tentative. "
                         "Handoff only inspects a current artifact and never executes."),
            emoji="",
        )
        return {"responsibility_tool": True}
    except Exception as exc:  # noqa: BLE001
        return {"responsibility_tool": False,
                "responsibility_tool_error": f"{type(exc).__name__}: {exc}"}


_NON_HUMAN = frozenset({
    "", "cron", "scheduler", "a2a", "agent", "subagent", "internal", "system",
    "worker", "background",
})


def _eligible_human_turn(platform: Any, user_message: Any, *, chat_type: str = "",
                         is_shared: bool = False) -> bool:
    platform_key = str(platform or "").strip().lower()
    if platform_key in _NON_HUMAN:
        return False
    if any(token in platform_key for token in ("cron", "a2a", "subagent", "internal")):
        return False
    if any(token in platform_key for token in ("group", "channel", "shared")):
        return False
    if is_shared or str(chat_type or "").strip().lower() in {"group", "channel", "supergroup"}:
        return False
    text = _text(user_message).strip()
    return bool(text) and not text.startswith("/")


def responsibility_context(ctx: Any, *, platform: Any, user_message: Any,
                           session_id: Any = "", chat_type: str = "",
                           is_shared: bool = False) -> str | None:
    """Store the human turn and offer exact capture instructions when relevant."""
    if not _eligible_human_turn(platform, user_message, chat_type=chat_type,
                                is_shared=is_shared):
        return None
    sid = _clean(session_id, 200)
    if not sid:
        return None
    turn = remember_human_turn(ctx, session_id=sid, user_message=user_message,
                               platform=platform)
    if turn is None or not is_commitment(turn.get("text")):
        return None
    return (
        "[Proactive Secretary responsibility]\n"
        "The user's current turn appears to contain a clear personal commitment. "
        "Use the secretary_responsibility tool with action=capture only if it really is a "
        "commitment the user made. Pass this exact session_id and quote the exact user "
        f"words in evidence_quote. Current session id: {sid}. "
        "If it is merely an inferred opportunity, use kind=opportunity (tentative) or say "
        "nothing. Do not capture thanks, silence, questions or assistant suggestions. "
        "Capture each human turn at most once; no external action is taken."
    )


__all__ = [
    "TURN_KEY", "TOOL_SCHEMA", "is_commitment", "remember_human_turn",
    "latest_turn", "capture", "dispatch", "tool_handler", "register_tool",
    "responsibility_context",
]
