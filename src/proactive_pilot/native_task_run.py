"""Bounded native task-run bridge over the existing Hermes CLI seam."""
from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

MAX_INSTRUCTIONS_CHARS = 100_000
MAX_OUTPUT_CHARS = 65_536
MAX_TOOLSETS = 32
MAX_EXPECTED_TOOLS = 512
TERM_GRACE_SECONDS = 2.0
KILL_GRACE_SECONDS = 2.0

DEFAULT_MAX_TURNS = 12
DEFAULT_RUN_BUDGET_SECONDS = 90
DEFAULT_TIMEOUT_SECONDS = 100
DEFAULT_RESOLVE_TIMEOUT_SECONDS = 30

_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TOOLSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TOOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ROUTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")

_BROAD_TOOLSETS = frozenset({"all", "everything", "*", "any", "all_tools"})
_TOOL_TOKEN_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|[A-Z]+|\d+"
)
_DENIED_TOOL_WORDS = frozenset({
    "shell", "terminal", "bash", "zsh", "exec", "command", "process",
    "filesystem", "file", "directory", "path", "read", "write", "edit",
    "patch", "mount", "mcp", "cron", "schedule", "send", "message", "mail",
    "smtp", "browser", "web", "http", "network", "request", "fetch",
    "socket", "curl", "wget", "python", "node", "code", "eval", "plugin",
    "service", "system", "desktop", "clipboard", "secret", "secrets",
    "credential", "credentials", "token", "keychain", "env", "install",
    "package", "approval", "hook",
})


class _Denied(Exception):
    pass


@dataclass(frozen=True)
class NativeTaskResult:
    status: str
    output: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    reason: str = ""
    toolsets: tuple[str, ...] = ()
    expected_tools: tuple[str, ...] = ()
    resolved_tools: tuple[str, ...] = ()
    route_provenance: str = "profile"
    model: str | None = None
    provider: str | None = None
    output_truncated: bool = False
    diagnostics: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return False


Resolver = Callable[..., tuple[bool, Sequence[str], str]]


def _default_resolver(
    toolset: str, *, hermes_bin: str, timeout: int
) -> tuple[bool, Sequence[str], str]:
    from .evaluator import resolve_toolset_tools

    return resolve_toolset_tools(toolset, hermes_bin=hermes_bin, timeout=timeout)


def _reject(reason: str) -> None:
    raise _Denied(reason)


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _reject(f"{name} must be a positive integer")
    return value


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(f"{name} must be a positive finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        _reject(f"{name} must be a positive finite number")
    return number


def _policy_bounds(policy: Any) -> tuple[int, int, float, int]:
    if not isinstance(policy, Mapping):
        _reject("policy must be a mapping")
    cfg = policy.get("evaluator", {})
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, Mapping):
        _reject("evaluator policy must be a mapping")
    max_turns = _positive_int(cfg.get("max_turns", DEFAULT_MAX_TURNS), "max_turns")
    run_budget = _positive_int(
        cfg.get("run_budget_seconds", DEFAULT_RUN_BUDGET_SECONDS),
        "run_budget_seconds",
    )
    timeout = _positive_number(
        cfg.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "timeout_seconds"
    )
    resolve_timeout = _positive_int(
        cfg.get("resolve_timeout_seconds", DEFAULT_RESOLVE_TIMEOUT_SECONDS),
        "resolve_timeout_seconds",
    )
    return max_turns, run_budget, timeout, resolve_timeout


def _validate_profile(profile: Any) -> str:
    if not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile):
        _reject("profile must be an exact validated literal")
    if ".." in profile:
        _reject("profile must be an exact validated literal")
    return profile


def _validate_toolsets(toolsets: Any) -> tuple[str, ...]:
    if isinstance(toolsets, (str, bytes)) or not isinstance(toolsets, Sequence):
        _reject("toolsets must be an explicit list of toolset names")
    if len(toolsets) > MAX_TOOLSETS:
        _reject("too many toolsets")
    names: list[str] = []
    for item in toolsets:
        if not isinstance(item, str) or not _TOOLSET_RE.fullmatch(item):
            _reject("toolset name is not an exact validated literal")
        if item == "no_mcp":
            _reject("no_mcp is an internal sentinel and must not be supplied")
        if item.lower() in _BROAD_TOOLSETS or "*" in item:
            _reject("broad toolsets are not allowed")
        names.append(item)
    if len(set(names)) != len(names):
        _reject("duplicate toolset names are not allowed")
    return tuple(names)


def _validate_expected_tools(expected_tools: Any) -> tuple[str, ...]:
    if expected_tools is None:
        _reject("expected_tools is required; use [] only for a zero-tool proof")
    if isinstance(expected_tools, (str, bytes)) or not isinstance(
        expected_tools, Sequence
    ):
        _reject("expected_tools must be an explicit list")
    if len(expected_tools) > MAX_EXPECTED_TOOLS:
        _reject("too many expected tools")
    names: list[str] = []
    for item in expected_tools:
        if not isinstance(item, str) or not _TOOL_RE.fullmatch(item):
            _reject("expected tool name is not an exact validated literal")
        if _tool_is_denied(item):
            _reject(f"expected tool {item!r} is outside the bounded task scope")
        names.append(item)
    if len(set(names)) != len(names):
        _reject("duplicate expected tool names are not allowed")
    return tuple(names)


def _validate_route(route: Any) -> tuple[str | None, str | None, str]:
    if route is None:
        return None, None, "profile"
    if isinstance(route, Mapping):
        if set(route) != {"model", "provider"}:
            _reject("route must contain exactly model and provider")
        model, provider = route.get("model"), route.get("provider")
    elif isinstance(route, (tuple, list)) and len(route) == 2:
        model, provider = route
    else:
        _reject("route must be an explicit trusted model/provider pair")
    if not isinstance(provider, str) or not _ROUTE_RE.fullmatch(provider):
        _reject("route provider is not an exact validated literal")
    if not isinstance(model, str) or not _ROUTE_RE.fullmatch(model):
        _reject("route model is not an exact validated literal")
    return model, provider, "explicit"


def _validate_instructions(instructions: Any) -> str:
    if not isinstance(instructions, str):
        _reject("instructions must be text")
    if not instructions.strip():
        _reject("instructions must not be empty")
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        _reject("instructions exceed the bounded size")
    return instructions


def _validate_hermes_bin(hermes_bin: Any) -> str:
    if not isinstance(hermes_bin, str) or not hermes_bin or "\x00" in hermes_bin:
        _reject("hermes_bin must be an exact executable path or name")
    return hermes_bin


def _tool_tokens(name: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for chunk in re.findall(r"[A-Za-z0-9]+", name):
        tokens.extend(match.lower() for match in _TOOL_TOKEN_RE.findall(chunk))
    return tuple(tokens)


def _tool_is_denied(name: str) -> bool:
    return any(token in _DENIED_TOOL_WORDS for token in _tool_tokens(name))


class NativeTaskRunner:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        popen_factory: Callable[..., Any] | None = None,
        killpg: Callable[[int, int], Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._resolver = resolver or _default_resolver
        self._popen = popen_factory or subprocess.Popen
        self._killpg = killpg or os.killpg
        self._clock = clock or time.monotonic

    def run(
        self,
        instructions: str,
        *,
        toolsets: Sequence[str],
        expected_tools: Sequence[str] | None,
        policy: Mapping[str, Any],
        profile: str = "default",
        hermes_bin: str = "hermes",
        before_dispatch: Callable[[], bool] | None = None,
        route: Any = None,
    ) -> NativeTaskResult:
        started = self._clock()
        toolsets_out: tuple[str, ...] = ()
        expected_out: tuple[str, ...] = ()
        route_provenance = "profile"
        model: str | None = None
        provider: str | None = None
        try:
            text = _validate_instructions(instructions)
            toolsets_out = _validate_toolsets(toolsets)
            expected_out = _validate_expected_tools(expected_tools)
            profile = _validate_profile(profile)
            hermes_bin = _validate_hermes_bin(hermes_bin)
            model, provider, route_provenance = _validate_route(route)
            max_turns, run_budget, timeout, resolve_timeout = _policy_bounds(policy)
            resolved = self._preflight(
                toolsets_out,
                expected_out,
                hermes_bin=hermes_bin,
                timeout=resolve_timeout,
            )
        except _Denied as exc:
            return self._result(
                "denied", started, reason=str(exc), toolsets=toolsets_out,
                expected_tools=expected_out, route_provenance=route_provenance,
                model=model, provider=provider,
            )

        argv = [hermes_bin, "-p", profile, "chat", "-Q", "--source", "tool"]
        argv.extend(["--run-budget", str(run_budget)])
        argv.extend(["--max-turns", str(max_turns)])
        if model is not None and provider is not None:
            argv.extend(["--provider", provider, "-m", model])
        argv.extend(["-t", ",".join((*toolsets_out, "no_mcp"))])
        argv.extend(["--query-file", "-"])

        if not callable(before_dispatch):
            return self._result(
                "denied", started, reason="before_dispatch callback is missing",
                toolsets=toolsets_out, expected_tools=expected_out,
                resolved_tools=resolved, route_provenance=route_provenance,
                model=model, provider=provider,
            )
        try:
            allowed = before_dispatch()
        except Exception as exc:  # noqa: BLE001 - never spawn after callback failure
            return self._result(
                "denied", started,
                reason=f"before_dispatch failed ({type(exc).__name__})",
                toolsets=toolsets_out, expected_tools=expected_out,
                resolved_tools=resolved, route_provenance=route_provenance,
                model=model, provider=provider,
            )
        if allowed is not True:
            return self._result(
                "denied", started, reason="before_dispatch denied dispatch",
                toolsets=toolsets_out, expected_tools=expected_out,
                resolved_tools=resolved, route_provenance=route_provenance,
                model=model, provider=provider,
            )

        try:
            proc = self._popen(
                argv,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            return self._result(
                "error", started, reason=f"native CLI spawn failed ({type(exc).__name__})",
                toolsets=toolsets_out, expected_tools=expected_out,
                resolved_tools=resolved, route_provenance=route_provenance,
                model=model, provider=provider,
            )

        try:
            stdout, stderr = proc.communicate(input=text, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate(proc)
            return self._result(
                "timeout", started, reason="native CLI exceeded its deadline",
                toolsets=toolsets_out, expected_tools=expected_out,
                resolved_tools=resolved, route_provenance=route_provenance,
                model=model, provider=provider,
                diagnostics=(f"timeout_seconds={timeout}", "process_group_terminated"),
            )
        except Exception as exc:  # noqa: BLE001 - clean up before reporting
            self._terminate(proc)
            return self._result(
                "error", started,
                reason=f"native CLI communication failed ({type(exc).__name__})",
                toolsets=toolsets_out, expected_tools=expected_out,
                resolved_tools=resolved, route_provenance=route_provenance,
                model=model, provider=provider,
            )

        output, truncated = self._retain(stdout)
        rc = proc.returncode
        diagnostics = (f"exit={rc}", f"stderr_chars={len(stderr or '')}")
        if rc == 0:
            return self._result(
                "executed_unverified", started, output=output, exit_code=rc,
                toolsets=toolsets_out, expected_tools=expected_out,
                resolved_tools=resolved, route_provenance=route_provenance,
                model=model, provider=provider, output_truncated=truncated,
                diagnostics=diagnostics,
            )
        return self._result(
            "error", started, output=output, exit_code=rc,
            reason=f"native CLI exited with status {rc}",
            toolsets=toolsets_out, expected_tools=expected_out,
            resolved_tools=resolved, route_provenance=route_provenance,
            model=model, provider=provider, output_truncated=truncated,
            diagnostics=diagnostics,
        )

    def _preflight(
        self,
        toolsets: tuple[str, ...],
        expected: tuple[str, ...],
        *,
        hermes_bin: str,
        timeout: int,
    ) -> tuple[str, ...]:
        actual: set[str] = set()
        for toolset in toolsets:
            try:
                outcome = self._resolver(
                    toolset, hermes_bin=hermes_bin, timeout=timeout
                )
            except Exception as exc:  # noqa: BLE001 - resolution never permissive
                _reject(
                    f"toolset resolution failed for {toolset!r} "
                    f"({type(exc).__name__})"
                )
            if (
                not isinstance(outcome, tuple)
                or len(outcome) != 3
            ):
                _reject(f"toolset resolver returned malformed data for {toolset!r}")
            ok, names, detail = outcome
            if ok is not True:
                safe_detail = str(detail).replace("\n", " ")[:200]
                _reject(
                    f"toolset resolution failed for {toolset!r}: {safe_detail}"
                )
            if isinstance(names, (str, bytes)) or not isinstance(names, Sequence):
                _reject(f"toolset resolver returned malformed names for {toolset!r}")
            for name in names:
                if not isinstance(name, str) or not _TOOL_RE.fullmatch(name):
                    _reject(f"toolset resolver returned an invalid tool name for {toolset!r}")
                if name == "no_mcp":
                    continue
                if _tool_is_denied(name):
                    _reject(f"unexpected tool exposure {name!r}")
                actual.add(name)
        resolved = tuple(sorted(actual))
        if resolved != tuple(sorted(set(expected))):
            _reject(
                "effective tool registry mismatch: "
                f"expected {sorted(set(expected))!r} resolved {list(resolved)!r}"
            )
        return resolved

    def _terminate(self, proc: Any) -> None:
        self._signal_group(proc, signal.SIGTERM)
        try:
            proc.communicate(timeout=TERM_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:  # noqa: BLE001 - still attempt the hard kill
            pass
        self._signal_group(proc, signal.SIGKILL)
        try:
            proc.communicate(timeout=KILL_GRACE_SECONDS)
        except Exception:  # noqa: BLE001 - bounded wait completed
            pass

    def _signal_group(self, proc: Any, sig: int) -> None:
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return
        try:
            self._killpg(pid, sig)
        except (ProcessLookupError, OSError):
            pass

    def _result(
        self,
        status: str,
        started: float,
        *,
        output: str = "",
        exit_code: int | None = None,
        reason: str = "",
        toolsets: tuple[str, ...] = (),
        expected_tools: tuple[str, ...] = (),
        resolved_tools: tuple[str, ...] = (),
        route_provenance: str = "profile",
        model: str | None = None,
        provider: str | None = None,
        output_truncated: bool = False,
        diagnostics: tuple[str, ...] = (),
    ) -> NativeTaskResult:
        duration = max(0, int((self._clock() - started) * 1000))
        return NativeTaskResult(
            status=status,
            output=output,
            exit_code=exit_code,
            duration_ms=duration,
            reason=reason,
            toolsets=toolsets,
            expected_tools=expected_tools,
            resolved_tools=resolved_tools,
            route_provenance=route_provenance,
            model=model,
            provider=provider,
            output_truncated=output_truncated,
            diagnostics=diagnostics,
        )

    def _retain(self, text: Any) -> tuple[str, bool]:
        if not isinstance(text, str):
            text = "" if text is None else str(text)
        if len(text) > MAX_OUTPUT_CHARS:
            return text[:MAX_OUTPUT_CHARS], True
        return text, False


__all__ = ["NativeTaskResult", "NativeTaskRunner"]
