"""Production consumer of the bounded native task runner.

The module deliberately contains no scheduler, model loop, provider client,
credential access or approval bypass. It validates an existing responsibility
claim through the existing store machinery, resolves the bounded preparation
brief, delegates exactly one execution to :class:`NativeTaskRunner`, verifies
the produced artifact before recording, and records a bounded outcome through
the existing responsibility mutation path.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from . import preparation
from . import responsibilities as R
from .native_task_run import (
    DEFAULT_MAX_TURNS,
    DEFAULT_RESOLVE_TIMEOUT_SECONDS,
    DEFAULT_RUN_BUDGET_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    NativeTaskRunner,
)
from .state import CorruptStateError

MAX_INSTRUCTION_CHARS = 64_000
MAX_AGENTIC_TURNS = 24
MAX_SUMMARY_CHARS = 1_000

_ABS_PATH = re.compile(r"(?:/[\w.~-]+){2,}/?")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_BEARER = re.compile(
    r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE)
_SECRET_ASSIGN = re.compile(
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"private[_-]?key|password|passwd|secret)\s*[:=]\s*[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE)
_SECRET_TOKEN = re.compile(r"\b(?:sk|ghp|gho|ghu|ghs|xox[baprs])[_-][A-Za-z0-9_-]{12,}")


class AgenticTaskRefused(RuntimeError):
    """A pre-execution refusal; the store and runner are left untouched."""


@dataclass(frozen=True)
class AgenticTaskOutcome:
    responsibility_id: str
    status: str
    verified: bool
    recorded: bool
    runner_status: str = ""
    reason: str = ""
    artifact_ref: str = ""
    artifact_hash: str = ""
    artifact_size: int = 0
    diagnostics: tuple[str, ...] = ()


class _VerificationFailed(Exception):
    pass


def _scrub(value: Any, token: Any) -> str:
    text = "" if value is None else str(value)
    token_text = "" if token is None else str(token)
    if token_text:
        text = text.replace(token_text, "[redacted]")
    text = _SECRET_BEARER.sub("[redacted]", text)
    text = _SECRET_ASSIGN.sub("[redacted]", text)
    text = _SECRET_TOKEN.sub("[redacted]", text)
    text = _ABS_PATH.sub("[path]", text)
    text = _CONTROL.sub(" ", text)
    text = " ".join(text.split())
    return text[:400]


def _safe_diagnostics(value: Any, token: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        items: Sequence[Any] = (value,)
    elif isinstance(value, Sequence):
        items = value
    else:
        items = (value,)
    out: list[str] = []
    for item in items[:16]:
        text = _scrub(item, token)
        if text:
            out.append(text[:200])
    return tuple(out)


def _summary(
    kind: str, detail: str, diagnostics: Sequence[str], token: Any
) -> str:
    pieces = [f"agentic task {kind}"]
    if detail:
        pieces.append(detail)
    if diagnostics:
        pieces.append("diagnostics: " + "; ".join(diagnostics))
    return _scrub(" | ".join(pieces), token)[:MAX_SUMMARY_CHARS]


def _record(
    store: Any,
    responsibility_id: str,
    claim_token: Any,
    *,
    generation: Any,
    source_version: Any,
    summary: str,
    artifact_refs: Any,
    now: datetime | None,
) -> bool:
    try:
        R.checkpoint(
            store,
            responsibility_id,
            claim_token=claim_token,
            generation=generation,
            source_version=source_version,
            summary=summary,
            artifact_refs=artifact_refs,
            now=now,
        )
        return True
    except Exception:  # noqa: BLE001 - recording must fail closed
        return False


def _fresh_now(store: Any, admitted_at: datetime) -> datetime:
    current = store.clock.now()
    return current if current >= admitted_at else admitted_at


def _claim_still_current(
    store: Any,
    responsibility_id: str,
    claim_token: Any,
    *,
    expected: dict[str, Any],
    admitted_at: datetime,
) -> bool:
    try:
        now = _fresh_now(store, admitted_at)
        packet = R.claimed_brief(
            store, responsibility_id, claim_token, now=now)
        current = R.inspect(store, responsibility_id, include_claim=True)
    except Exception:  # noqa: BLE001 - an unreadable fence fails closed
        return False
    if current.get("status") != R.ACCEPTED:
        return False
    if int(packet.get("generation", -1)) != int(expected.get("generation", 0)):
        return False
    if int(current.get("generation", -1)) != int(expected.get("generation", 0)):
        return False
    if str(packet.get("source_version", "")) != str(expected.get("source_version", "")):
        return False
    if str(current.get("source_version", "")) != str(expected.get("source_version", "")):
        return False
    claim = current.get("claim") or {}
    original = expected.get("claim") or {}
    if str(claim.get("token", "")) != str(original.get("token", "")):
        return False
    if str(claim.get("expires_at", "")) != str(original.get("expires_at", "")):
        return False
    if int(claim.get("generation", -1)) != int(original.get("generation", -1)):
        return False
    if str(claim.get("source_version", "")) != str(original.get("source_version", "")):
        return False
    return True


def _validate_max_turns(max_turns: Any) -> int:
    if isinstance(max_turns, bool) or not isinstance(max_turns, int):
        raise AgenticTaskRefused("max_turns must be a positive integer")
    if max_turns < 1 or max_turns > MAX_AGENTIC_TURNS:
        raise AgenticTaskRefused("max_turns is outside the bounded range")
    return max_turns


def _build_instructions(
    store: Any,
    responsibility_id: str,
    claim_token: Any,
    brief: dict[str, Any],
    record: dict[str, Any],
    now: datetime | None,
) -> str:
    claim = {
        "claim_token": claim_token,
        "generation": record["generation"],
        "source_version": record["source_version"],
        "responsibility": record,
    }
    try:
        context = preparation.build_context(
            claim, list(brief.get("facts") or []))
        public = preparation.public_context(context)
        instructions = (
            str(public.get("instructions") or "")
            + "\n\nBOUNDED TASK DATA:\n"
            + json.dumps(public.get("data") or {}, ensure_ascii=False, sort_keys=True)
        )
    except Exception:  # noqa: BLE001 - instruction resolution fails closed
        raise AgenticTaskRefused("task instructions could not be resolved") from None
    if not instructions.strip():
        raise AgenticTaskRefused("task instructions are empty")
    if len(instructions) > MAX_INSTRUCTION_CHARS:
        raise AgenticTaskRefused("task instructions exceed the bounded size")
    return instructions


def _verify_transition(
    store: Any,
    responsibility_id: str,
    claim_token: Any,
    *,
    before: dict[str, Any],
    now: datetime | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        R.claimed_brief(store, responsibility_id, claim_token, now=now)
    except Exception:  # noqa: BLE001 - verification fails closed
        raise _VerificationFailed("claim is no longer valid for recording") from None
    try:
        after = R.inspect(store, responsibility_id, include_claim=True)
    except Exception:  # noqa: BLE001 - verification fails closed
        raise _VerificationFailed("responsibility could not be re-read") from None
    if after.get("status") != R.ACCEPTED:
        raise _VerificationFailed("responsibility is no longer accepted")
    if int(after.get("generation", 0)) != int(before.get("generation", 0)):
        raise _VerificationFailed("responsibility generation changed during execution")
    if str(after.get("source_version", "")) != str(before.get("source_version", "")):
        raise _VerificationFailed("source version changed during execution")
    ready = after.get("ready") or {}
    if ready.get("status") != "queued":
        raise _VerificationFailed("expected preparation artifact was not queued")
    ref = str(ready.get("artifact_ref") or "")
    digest = str(ready.get("hash") or "")
    if not ref or not digest:
        raise _VerificationFailed("queued artifact identity is incomplete")
    before_ready = before.get("ready") or {}
    unchanged = (
        before_ready.get("status") == "queued"
        and str(before_ready.get("artifact_ref") or "") == ref
        and str(before_ready.get("hash") or "") == digest
    )
    if unchanged:
        raise _VerificationFailed("no new artifact transition was observed")
    try:
        handoff = preparation.foreground_handoff(
            store, responsibility_id, now=now)
    except Exception:  # noqa: BLE001 - verification fails closed
        raise _VerificationFailed("artifact readback verification failed") from None
    if str(handoff.get("artifact_ref") or "") != ref:
        raise _VerificationFailed("verified artifact reference does not match")
    if str(handoff.get("artifact_hash") or "") != digest:
        raise _VerificationFailed("verified artifact hash does not match")
    current = handoff.get("current_version") or {}
    if int(current.get("generation", -1)) != int(after.get("generation", 0)):
        raise _VerificationFailed("artifact generation does not match")
    if str(current.get("source_version", "")) != str(after.get("source_version", "")):
        raise _VerificationFailed("artifact source version does not match")
    return after, handoff


def run_agentic_task(
    store: Any,
    responsibility_id: str,
    *,
    claim_token: Any,
    runner: Any = None,
    gate_fn: Any = None,
    toolsets: Any,
    expected_tools: Any,
    max_turns: int = DEFAULT_MAX_TURNS,
    now: datetime | None = None,
) -> AgenticTaskOutcome:
    """Execute one claimed preparation task at most once, then verify and record.

    All refusals raise :class:`AgenticTaskRefused` before any runner call or
    store mutation. Runtime denials/timeouts/verification failures return a
    non-success outcome and never mark the responsibility successful.
    """
    admitted_at = now if now is not None else store.clock.now()

    try:
        brief = R.claimed_brief(
            store, responsibility_id, claim_token, now=admitted_at)
    except (R.ResponsibilityError, CorruptStateError) as exc:
        raise AgenticTaskRefused(_scrub(exc, claim_token)) from None

    try:
        preparation.check_gate(gate_fn)
    except R.ResponsibilityError as exc:
        raise AgenticTaskRefused(_scrub(exc, claim_token)) from None
    except Exception as exc:  # noqa: BLE001 - an unavailable gate fails closed
        raise AgenticTaskRefused(
            f"preparation gate unavailable: {type(exc).__name__}") from None

    turns = _validate_max_turns(max_turns)

    try:
        before = R.inspect(store, responsibility_id, include_claim=True)
    except (R.ResponsibilityError, CorruptStateError) as exc:
        raise AgenticTaskRefused(_scrub(exc, claim_token)) from None

    instructions = _build_instructions(
        store, responsibility_id, claim_token, brief, before, now)

    if runner is None:
        runner = NativeTaskRunner()
    if not callable(getattr(runner, "run", None)):
        raise AgenticTaskRefused("native task runner is unavailable")

    policy = {
        "evaluator": {
            "max_turns": turns,
            "run_budget_seconds": DEFAULT_RUN_BUDGET_SECONDS,
            "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
            "resolve_timeout_seconds": DEFAULT_RESOLVE_TIMEOUT_SECONDS,
        }
    }

    def _before_dispatch() -> bool:
        try:
            preparation.check_gate(gate_fn)
        except Exception:  # noqa: BLE001 - denial is handled by the runner
            return False
        return _claim_still_current(
            store, responsibility_id, claim_token,
            expected=before, admitted_at=admitted_at)

    try:
        result = runner.run(
            instructions,
            toolsets=toolsets,
            expected_tools=expected_tools,
            policy=policy,
            before_dispatch=_before_dispatch,
        )
    except Exception as exc:  # noqa: BLE001 - one attempt only, no retry
        diagnostics: tuple[str, ...] = ()
        reason = f"native task runner raised {type(exc).__name__}"
        summary = _summary("non-success", reason, diagnostics, claim_token)
        recorded = _record(
            store, responsibility_id, claim_token,
            generation=before["generation"],
            source_version=before["source_version"],
            summary=summary, artifact_refs=None,
            now=_fresh_now(store, admitted_at))
        return AgenticTaskOutcome(
            responsibility_id=str(responsibility_id),
            status="error",
            verified=False,
            recorded=recorded,
            runner_status="",
            reason=_scrub(reason, claim_token),
            diagnostics=diagnostics,
        )

    raw_status = getattr(result, "status", "")
    runner_status = raw_status if isinstance(raw_status, str) else ""
    diagnostics = _safe_diagnostics(getattr(result, "diagnostics", ()), claim_token)
    raw_reason = getattr(result, "reason", "")

    if runner_status != "executed_unverified":
        status = {
            "denied": "denied",
            "timeout": "timeout",
            "error": "error",
        }.get(runner_status, "error")
        reason = _scrub(
            raw_reason or f"native task runner returned {runner_status or 'no status'}",
            claim_token,
        )
        summary = _summary("non-success", reason, diagnostics, claim_token)
        recorded = _record(
            store, responsibility_id, claim_token,
            generation=before["generation"],
            source_version=before["source_version"],
            summary=summary, artifact_refs=None,
            now=_fresh_now(store, admitted_at))
        return AgenticTaskOutcome(
            responsibility_id=str(responsibility_id),
            status=status,
            verified=False,
            recorded=recorded,
            runner_status=_scrub(runner_status, claim_token),
            reason=reason,
            diagnostics=diagnostics,
        )

    try:
        after, handoff = _verify_transition(
            store, responsibility_id, claim_token, before=before,
            now=_fresh_now(store, admitted_at))
    except _VerificationFailed as exc:
        reason = _scrub(f"artifact verification failed: {exc}", claim_token)
        summary = _summary("non-success", reason, diagnostics, claim_token)
        recorded = _record(
            store, responsibility_id, claim_token,
            generation=before["generation"],
            source_version=before["source_version"],
            summary=summary, artifact_refs=None,
            now=_fresh_now(store, admitted_at))
        return AgenticTaskOutcome(
            responsibility_id=str(responsibility_id),
            status="unverified",
            verified=False,
            recorded=recorded,
            runner_status=_scrub(runner_status, claim_token),
            reason=reason,
            diagnostics=diagnostics,
        )

    artifact_ref = str(handoff.get("artifact_ref") or "")
    artifact_hash = str(handoff.get("artifact_hash") or "")
    artifact_size = int((after.get("ready") or {}).get("size") or 0)
    summary = _summary(
        "verified", "prepared artifact readback matched", diagnostics, claim_token)
    recorded = _record(
        store, responsibility_id, claim_token,
        generation=after["generation"],
        source_version=after["source_version"],
        summary=summary,
        artifact_refs=[artifact_ref] if artifact_ref else None,
        now=_fresh_now(store, admitted_at),
    )
    return AgenticTaskOutcome(
        responsibility_id=str(responsibility_id),
        status="success" if recorded else "unrecorded",
        verified=True,
        recorded=recorded,
        runner_status=_scrub(runner_status, claim_token),
        reason=_scrub(
            "prepared artifact verified" if recorded
            else "prepared artifact verified but the outcome could not be recorded",
            claim_token,
        ),
        artifact_ref=preparation.public_artifact_ref(artifact_ref),
        artifact_hash=artifact_hash,
        artifact_size=artifact_size,
        diagnostics=diagnostics,
    )


__all__ = ["AgenticTaskOutcome", "AgenticTaskRefused", "run_agentic_task"]
