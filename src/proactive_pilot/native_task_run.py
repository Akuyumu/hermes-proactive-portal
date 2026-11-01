"""Bounded native task-run bridge over the existing Hermes CLI seam."""
from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

MAX_INSTRUCTIONS_CHARS = 100_000
MAX_OUTPUT_CHARS = 65_536
MAX_TOOLSETS = 32
MAX_EXPECTED_TOOLS = 512
TERM_GRACE_SECONDS = 2.0
KILL_GRACE_SECONDS = 2.0
GROUP_POLL_SECONDS = 0.02

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
    toolsets: Sequence[str],
    *,
    hermes_bin: str,
    timeout: int,
    profile: str,
) -> tuple[bool, Sequence[str], str]:
    from .evaluator import resolve_toolset_tools

    return resolve_toolset_tools(
        None,
        hermes_bin=hermes_bin,
        timeout=timeout,
        toolsets=toolsets,
        profile=profile,
    )


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


def _default_group_probe(pid: int) -> bool:
    """True while any process remains in the owned pgid (leader may be gone)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # EPERM/EIO/etc. mean the group cannot be disproven, not that it is gone.
        return True


class NativeTaskRunner:
    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        popen_factory: Callable[..., Any] | None = None,
        killpg: Callable[[int, int], Any] | None = None,
        group_probe: Callable[[int], bool] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._resolver = resolver or _default_resolver
        self._popen = popen_factory or subprocess.Popen
        self._killpg = killpg or os.killpg
        self._group_probe = group_probe or _default_group_probe
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
                profile=profile,
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
            stdout, stderr, rc, timed_out, terminated, io_errors, collected_truncated = (
                self._collect(proc, text, timeout)
            )
        except Exception as exc:  # noqa: BLE001 - clean up before reporting
            _terminated, _detail = self._terminate(proc)
            return self._result(
                "error", started,
                reason=f"native CLI communication failed ({type(exc).__name__})",
                toolsets=toolsets_out, expected_tools=expected_out,
                resolved_tools=resolved, route_provenance=route_provenance,
                model=model, provider=provider,
            )

        output, truncated = self._retain(stdout)
        truncated = truncated or collected_truncated
        if timed_out:
            diagnostics = [f"timeout_seconds={timeout}"]
            diagnostics.append(
                "process_group_terminated" if terminated
                else "process_group_termination_unconfirmed"
            )
            return self._result(
                "timeout", started, output=output,
                reason="native CLI exceeded its deadline",
                toolsets=toolsets_out, expected_tools=expected_out,
                resolved_tools=resolved, route_provenance=route_provenance,
                model=model, provider=provider, output_truncated=truncated,
                diagnostics=tuple(diagnostics),
            )
        if io_errors:
            diagnostics = [f"exit={rc}", f"stderr_chars={len(stderr)}"]
            diagnostics.append(f"io_error={io_errors[0]}")
            return self._result(
                "error", started, output=output, exit_code=rc,
                reason="native CLI stream collection failed",
                toolsets=toolsets_out, expected_tools=expected_out,
                resolved_tools=resolved, route_provenance=route_provenance,
                model=model, provider=provider, output_truncated=truncated,
                diagnostics=tuple(diagnostics),
            )
        diagnostics = (f"exit={rc}", f"stderr_chars={len(stderr)}")
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
        profile: str,
        hermes_bin: str,
        timeout: int,
    ) -> tuple[str, ...]:
        # Resolve the COMBINED explicit selection exactly once, even when it is
        # empty. An empty list is a real zero-tool assertion, never a skip that
        # would let the ambient/default resolver stand in for it.
        try:
            outcome = self._resolver(
                tuple(toolsets),
                hermes_bin=hermes_bin,
                timeout=timeout,
                profile=profile,
            )
        except Exception as exc:  # noqa: BLE001 - resolution never permissive
            _reject(f"effective tool resolution failed ({type(exc).__name__})")
        if not isinstance(outcome, tuple) or len(outcome) != 3:
            _reject("effective tool resolver returned malformed data")
        ok, names, detail = outcome
        if ok is not True:
            safe_detail = str(detail).replace("\n", " ")[:200]
            _reject(f"effective tool resolution failed: {safe_detail}")
        if isinstance(names, (str, bytes)) or not isinstance(names, Sequence):
            _reject("effective tool resolver returned malformed names")
        actual: set[str] = set()
        for name in names:
            if not isinstance(name, str) or not _TOOL_RE.fullmatch(name):
                _reject("effective tool resolver returned an invalid tool name")
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

    def _collect(
        self, proc: Any, text: str, timeout: float
    ) -> tuple[str, str, int | None, bool, bool, tuple[str, ...], bool]:
        """Collect bounded stdout/stderr while the child runs.

        Output is capped AS IT IS READ; the pipes are still drained to EOF so a
        verbose child cannot deadlock. The deadline covers every I/O thread and
        the child wait, and cleanup reports confirmed exit only when the owned
        process group actually exited.
        """
        out_holder: list[tuple[str, int, Exception | None]] = []
        err_holder: list[tuple[str, int, Exception | None]] = []

        def _reader(stream: Any, holder: list) -> None:
            chunks: list[str] = []
            kept = 0
            total = 0
            error: Exception | None = None
            try:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        break
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode("utf-8", "replace")
                    total += len(chunk)
                    if kept < MAX_OUTPUT_CHARS:
                        take = min(MAX_OUTPUT_CHARS - kept, len(chunk))
                        chunks.append(chunk[:take])
                        kept += take
            except Exception as exc:  # noqa: BLE001 - surfaced below
                error = exc
            finally:
                try:
                    stream.close()
                except Exception:
                    pass
            holder.append(("".join(chunks), total, error))

        threads: list[threading.Thread] = []
        for stream, holder in (
            (getattr(proc, "stdout", None), out_holder),
            (getattr(proc, "stderr", None), err_holder),
        ):
            if stream is not None:
                thread = threading.Thread(
                    target=_reader, args=(stream, holder), daemon=True
                )
                thread.start()
                threads.append(thread)

        stdin = getattr(proc, "stdin", None)
        if stdin is not None:
            def _writer() -> None:
                try:
                    stdin.write(text)
                    stdin.flush()
                except Exception:
                    pass
                finally:
                    try:
                        stdin.close()
                    except Exception:
                        pass

            writer = threading.Thread(target=_writer, daemon=True)
            writer.start()
            threads.append(writer)

        deadline = self._clock() + timeout
        for thread in threads:
            thread.join(max(0.0, deadline - self._clock()))
        timed_out = any(thread.is_alive() for thread in threads)

        rc: int | None = None
        if not timed_out:
            try:
                rc = proc.wait(timeout=max(0.0, deadline - self._clock()))
            except subprocess.TimeoutExpired:
                timed_out = True
            except Exception:
                timed_out = True

        terminated = False
        if timed_out:
            terminated, _detail = self._terminate(proc)
            for thread in threads:
                thread.join(0.2)
            rc = getattr(proc, "returncode", None)

        out = out_holder[0][0] if out_holder else ""
        err = err_holder[0][0] if err_holder else ""
        errors = tuple(
            f"{kind}:{type(holder[0][2]).__name__}"
            for kind, holder in (("stdout", out_holder), ("stderr", err_holder))
            if holder and holder[0][2] is not None
        )
        out_total = out_holder[0][1] if out_holder else 0
        err_total = err_holder[0][1] if err_holder else 0
        truncated = out_total > len(out) or err_total > len(err)
        return out, err, rc, timed_out, terminated, errors, truncated

    def _group_alive(self, proc: Any) -> bool:
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        try:
            return self._group_probe(pid) is True
        except Exception:
            return True  # cannot prove demise: stay conservative

    def _wait_group_gone(self, proc: Any, grace: float) -> bool:
        deadline = time.monotonic() + grace
        while True:
            try:
                proc.poll()  # reap an exited leader so it cannot hold the group open
            except Exception:
                pass
            if not self._group_alive(proc):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(GROUP_POLL_SECONDS, remaining))

    def _terminate(self, proc: Any) -> tuple[bool, str]:
        """Terminate the OWNED GROUP; confirmed only after the group is gone."""
        if not self._group_alive(proc):
            return True, "already_exited"
        for sig, grace, label in (
            (signal.SIGTERM, TERM_GRACE_SECONDS, "sigterm"),
            (signal.SIGKILL, KILL_GRACE_SECONDS, "sigkill"),
        ):
            if not self._group_alive(proc):
                return True, "already_exited"
            self._signal_group(proc, sig)
            if self._wait_group_gone(proc, grace):
                return True, label
        return False, "unconfirmed"

    def _signal_group(self, proc: Any, sig: int) -> bool:
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        try:
            self._killpg(pid, sig)
            return True
        except (ProcessLookupError, OSError):
            return False

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
