"""Native-first runtime bridge: plugin settings + the supported ``ctx.llm`` lane.

This module is the single place the ordinary pipeline talks to native Hermes.
It is intentionally duck-typed and never imports the Hermes runtime at module
import time, and it NEVER constructs ``PluginContext`` or ``PluginLlm`` itself:
the host-built context is passed in from the registered CLI handler closure or
from the scheduled runner, so normal discovery/consent and the per-plugin trust
gate stay authoritative.

Guarantees:

* Model calls go only through the host-owned ``ctx.llm`` facade, defaulting to
  the user's active provider/model. No provider/model/agent/profile override is
  ever requested, so no trust flag is needed and none can be widened.
* Every call is attributable: we pass a ``purpose`` and record which route was
  used (``ctx.llm`` vs ``unsupported``) without capturing raw prompts/secrets.
* The reminder dispatcher (and every known disabled/paused path) is
  model-free: building a route for those paths yields an unavailable route and
  makes zero calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NATIVE_PLUGIN_ID = "hermes-proactive-secretary"
# Paths that must never spend a model call, ever.
MODEL_FREE_PATHS = frozenset({"reminder", "disabled", "paused", "public-only"})


class LlmUnavailable(RuntimeError):
    """No supported host-owned model route is available for this call."""


def has_native_llm(ctx: Any) -> bool:
    """True when the injected context exposes the supported ``ctx.llm`` facade."""
    if ctx is None:
        return False
    try:
        llm = getattr(ctx, "llm", None)
    except Exception:  # noqa: BLE001 - a property error means "unavailable"
        return False
    return llm is not None and hasattr(llm, "complete_structured")


def read_native_settings(ctx: Any, key: str, default: Any = None) -> Any:
    """Read one plugin setting through the supported ``ctx.get_config`` accessor.

    Returns ``default`` when the context is absent or the read fails closed.
    """
    if ctx is None:
        return default
    getter = getattr(ctx, "get_config", None)
    if not callable(getter):
        return default
    try:
        return getter(key, default)
    except Exception:  # noqa: BLE001 - a rejected/failed read is "unset"
        return default


def native_capabilities(ctx: Any) -> dict[str, Any]:
    """Capability report for diagnostics; no secret or token is read."""
    return {
        "context": ctx is not None,
        "plugin_settings": ctx is not None and callable(getattr(ctx, "get_config", None)),
        "llm": has_native_llm(ctx),
    }


@dataclass
class LlmRoute:
    """A resolved, bounded, host-owned route (or an explicit unavailability)."""

    kind: str                      # "ctx.llm" | "unsupported"
    reason: str = ""
    ctx: Any = None
    purpose: str = "secretary"
    path: str = "evaluator"
    _calls: int = field(default=0, init=False, repr=False)

    @property
    def available(self) -> bool:
        return self.kind == "ctx.llm" and has_native_llm(self.ctx)

    @property
    def calls(self) -> int:
        return self._calls

    def describe(self) -> dict[str, Any]:
        """Route trace for status/diagnostics: no prompt or secret is included."""
        return {
            "route": self.kind,
            "purpose": self.purpose,
            "path": self.path,
            "available": self.available,
            "reason": self.reason,
            "model_calls": self._calls,
        }

    def complete_structured(
        self,
        *,
        instructions: str,
        input: list[dict[str, Any]],
        json_schema: dict[str, Any] | None = None,
        schema_name: str | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
    ) -> Any:
        """Run one bounded structured call through ``ctx.llm``.

        Never passes provider/model/agent_id/profile. Raises
        :class:`LlmUnavailable` (with a sanitized reason) when no route exists.
        """
        if not self.available:
            raise LlmUnavailable(self.reason or "no supported ctx.llm route")
        kwargs: dict[str, Any] = {
            "instructions": instructions,
            "input": input,
            "json_schema": json_schema,
            "json_mode": json_schema is None,
            "schema_name": schema_name,
            "system_prompt": system_prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": timeout,
            "purpose": self.purpose,
        }
        kwargs = {k: v for k, v in kwargs.items() if v is not None or k in {
            "instructions", "input", "json_schema", "json_mode", "purpose"}}
        self._calls += 1
        try:
            return self.ctx.llm.complete_structured(**kwargs)
        except LlmUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - sanitized, never leaks provider text
            raise LlmUnavailable(
                f"ctx.llm call failed ({type(exc).__name__})"
            ) from exc


def build_llm_route(
    ctx: Any,
    policy: dict[str, Any] | None = None,
    *,
    path: str = "evaluator",
    purpose: str = "secretary",
) -> LlmRoute:
    """Resolve the model route for one pipeline path, fail-closed.

    ``path`` in :data:`MODEL_FREE_PATHS` always yields an unavailable route, so a
    reminder/disabled/paused path can never spend a model call even if a context
    is mistakenly supplied.
    """
    if path in MODEL_FREE_PATHS:
        return LlmRoute(kind="unsupported", ctx=None, path=path, purpose=purpose,
                        reason=f"model-free path: {path}")
    if not has_native_llm(ctx):
        return LlmRoute(kind="unsupported", ctx=None, path=path, purpose=purpose,
                        reason="host contexts does not expose ctx.llm"
                        if ctx is not None else "no host context supplied")
    return LlmRoute(kind="ctx.llm", ctx=ctx, path=path, purpose=purpose,
                    reason="host-owned active model")


def evaluator_route(ctx: Any, policy: dict[str, Any] | None = None) -> LlmRoute:
    """Route for the ordinary evaluator/planner/proposer path."""
    return build_llm_route(ctx, policy, path="evaluator", purpose="secretary.evaluate")


def reminder_route(policy: dict[str, Any] | None = None) -> LlmRoute:
    """The reminder dispatcher is deterministic and model-free by construction."""
    return build_llm_route(None, policy, path="reminder", purpose="secretary.reminder")


@dataclass
class NativeModelLane:
    """A reusable, bounded JSON extraction lane over one ``ctx.llm`` route.

    Every model lane (evaluator, planner, inferred-interest proposer, semantic
    privacy verifier, conversational intake) uses this single wrapper, so the
    host-owned trust boundary, purpose attribution and sanitized failure
    behaviour are identical everywhere. It never requests a provider/model/agent/
    profile override and never sees a credential.
    """

    route: LlmRoute

    @property
    def available(self) -> bool:
        return self.route.available

    def describe(self) -> dict[str, Any]:
        return self.route.describe()

    def complete_json(
        self,
        *,
        instructions: str,
        system_prompt: str | None = None,
        schema_name: str | None = None,
        purpose: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Run one bounded structured call and return the parsed JSON object."""
        if not self.available:
            raise LlmUnavailable(self.route.reason or "no supported ctx.llm route")
        from .evaluator import _extract_json

        result = self.route.complete_structured(
            instructions=instructions,
            input=[{"type": "text", "text": "Return only the JSON object."}],
            schema_name=schema_name,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            timeout=timeout,
            temperature=temperature,
        )
        parsed = getattr(result, "parsed", None)
        text = getattr(result, "text", "") or ""
        obj = parsed if isinstance(parsed, dict) else _extract_json(text)
        if obj is None:
            raise LlmUnavailable("ctx.llm returned no parseable JSON object")
        return obj


def build_model_lane(
    ctx: Any,
    policy: dict[str, Any] | None = None,
    *,
    path: str = "model",
    purpose: str = "secretary.model",
) -> NativeModelLane:
    """Resolve a reusable JSON lane for one pipeline path (fail-closed)."""
    return NativeModelLane(build_llm_route(ctx, policy, path=path, purpose=purpose))


__all__ = [
    "NATIVE_PLUGIN_ID", "MODEL_FREE_PATHS", "LlmUnavailable", "LlmRoute",
    "NativeModelLane", "has_native_llm", "read_native_settings", "native_capabilities",
    "build_llm_route", "build_model_lane", "evaluator_route", "reminder_route",
]
