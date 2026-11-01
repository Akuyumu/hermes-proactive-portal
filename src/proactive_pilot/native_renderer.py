"""Constrained, opt-in native public renderer (plugin-owned adapter).

This module is the plugin-owned adapter used ONLY when the owner has enabled
BOTH ``native_render_fallback_enabled`` and ``native_render_consent``. It does
not reuse the owner's Hermes browser/session and it invents no native host API.

Architecture:

* :class:`NativePublicRenderer` runs one isolated worker process through
  ``sys.executable``. Only the public URL and bounded, non-secret limits are
  passed to that process (on stdin, never through inherited credentials).
* The worker (:mod:`proactive_pilot.render_worker`) owns a fresh, non-persistent
  Playwright-managed Chromium context. Every browser document, redirect and
  subresource request is forced through the existing
  :class:`~proactive_pilot.publictransport.PinnedHTTPSTransport`; the browser
  itself is configured to fail closed (dead proxy, no DNS escape, QUIC/WebRTC
  disabled).
* The parent enforces a hard overall deadline, bounded stdout, and tears down
  the whole owned process group on timeout/crash.

Honesty: cheap availability checks never open a browser, touch the store, make a
network/model call, or probe dependencies. If Playwright or a usable Chromium is
absent, a real render attempt fails with an explicit unavailable error instead of
silently substituting the owner browser or fabricating content.
"""
from __future__ import annotations

import base64
import json
import math
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import publicfeeds as pf

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_REQUESTS = 64
DEFAULT_MAX_NETWORK_BYTES = 4 * 1024 * 1024
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_STDERR_BYTES = 8 * 1024
_WORKER_OUTPUT_OVERHEAD = 16 * 1024

# Only non-secret runtime/dependency variables may cross into the worker. No
# credential/API-key/token/session variable is inherited, even if the parent
# happens to have one set.
_ALLOWED_CHILD_ENV = (
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PLAYWRIGHT_BROWSERS_PATH",
    "LD_LIBRARY_PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "SYSTEMROOT",
    "WINDIR",
)


class RenderUnavailable(pf.FeedError):
    """No safe bounded render could be produced (candid failure)."""


class _TimedOut(Exception):
    pass


class _OutputExceeded(Exception):
    pass


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _positive_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def resolve_native_render_config(policy: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise the bounded renderer limits; neutral and off by default."""
    p = policy or {}
    return {
        "enabled": bool(p.get("native_render_fallback_enabled", False)),
        "consent": bool(p.get("native_render_consent", False)),
        "timeout_seconds": _positive_int(
            p.get("native_render_timeout_seconds"), DEFAULT_TIMEOUT_SECONDS),
        "max_output_bytes": _positive_int(
            p.get("retrieval_fallback_max_bytes"), pf.DEFAULT_MAX_BYTES),
        "max_requests": _positive_int(
            p.get("native_render_max_requests"), DEFAULT_MAX_REQUESTS),
        "max_network_bytes": _positive_int(
            p.get("native_render_max_network_bytes"), DEFAULT_MAX_NETWORK_BYTES),
        "tmpdir": str(p.get("native_render_tmpdir") or "").strip(),
    }


def native_renderer_availability(policy: dict[str, Any] | None) -> tuple[bool, str]:
    """``(available, reason)`` for the built-in isolated renderer.

    Gated exactly by the existing owner booleans. This is deliberately cheap:
    no browser launch, dependency import/probe, store read, or network/model
    call happens here. A missing browser dependency surfaces honestly when a
    real render is attempted, not as a silent substitution.
    """
    cfg = resolve_native_render_config(policy)
    if not cfg["enabled"]:
        return False, "native render fallback disabled by policy"
    if not cfg["consent"]:
        return False, "native render fallback requires explicit owner consent"
    return True, ("configured/eligible isolated public renderer; dependency health "
                  "is not verified by this cheap check (a real render fails "
                  "honestly if Playwright/Chromium is unavailable)")


def build_native_renderer(policy: dict[str, Any] | None) -> "NativePublicRenderer | None":
    """Build the production renderer ONLY when both owner flags are enabled.

    Returns ``None`` for the neutral default so ordinary collection is unchanged.
    """
    cfg = resolve_native_render_config(policy)
    if not cfg["enabled"] or not cfg["consent"]:
        return None
    return NativePublicRenderer(policy)


def _child_env(tmpdir: str, *, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal allowlisted worker environment with a temporary home/profile.

    Explicitly preserves trusted dependency paths (notably Playwright's browser
    cache) while never inheriting auth/API keys or the owner profile.
    """
    source = dict(os.environ if environ is None else environ)
    env = {key: source[key] for key in _ALLOWED_CHILD_ENV if key in source}
    env.pop("PYTHONSTARTUP", None)
    if "PLAYWRIGHT_BROWSERS_PATH" not in env:
        home = source.get("HOME")
        if home:
            candidate = Path(home) / ".cache" / "ms-playwright"
            if candidate.is_dir():
                env["PLAYWRIGHT_BROWSERS_PATH"] = str(candidate)
    temp = str(tmpdir)
    env.update({
        "HOME": temp,
        "TMPDIR": temp,
        "TMP": temp,
        "TEMP": temp,
        "XDG_CONFIG_HOME": os.path.join(temp, "config"),
        "XDG_CACHE_HOME": os.path.join(temp, "cache"),
        "XDG_DATA_HOME": os.path.join(temp, "data"),
        "XDG_STATE_HOME": os.path.join(temp, "state"),
    })
    return env


def _safe_error(value: Any) -> str:
    text = str(value or "native render failed").strip()
    return text[:200] or "native render failed"


def _parse_worker_output(raw: bytes, limit: int) -> bytes:
    """Decode the worker's bounded structured result without trusting its text."""
    try:
        result = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise RenderUnavailable("native render worker returned an invalid result") from exc
    if not isinstance(result, dict):
        raise RenderUnavailable("native render worker returned an invalid result")
    if not result.get("ok"):
        raise RenderUnavailable(_safe_error(result.get("error")))
    encoded = result.get("body_b64")
    if not isinstance(encoded, str):
        raise RenderUnavailable("native render worker returned no content")
    try:
        data = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, TypeError) as exc:
        raise RenderUnavailable("native render worker returned malformed content") from exc
    if not data:
        raise RenderUnavailable("native renderer returned no content")
    return data[: max(1, int(limit))]


def _terminate_owned(proc: Any, *, pgid: int | None = None,
                     grace: float = 1.0) -> None:
    """Terminate/kill every member of the OWNED worker group, then reap.

    The group id is captured at spawn (``start_new_session`` makes it equal to
    the worker pid). It is signalled even if the group leader has already
    exited, so a stubborn descendant cannot survive. Broad process-name matching
    is never used and the current process group is never signalled.
    """
    if proc is None:
        return
    try:
        own_group = os.getpgrp()
    except OSError:  # pragma: no cover - defensive
        own_group = None
    owned_group = pgid
    if owned_group is None:
        pid = getattr(proc, "pid", None)
        if pid:
            try:
                candidate = os.getpgid(int(pid))
            except (OSError, TypeError, ValueError):
                candidate = None
            # Only trust a group we created and that is not our own group.
            if candidate == int(pid) and candidate != own_group:
                owned_group = candidate

    def _signal(sig: int) -> None:
        try:
            os.killpg(int(owned_group), sig)
        except (OSError, TypeError, ValueError):
            pass

    if owned_group is not None and owned_group != own_group:
        # TERM the whole group first; wait for the leader (if any); then KILL the
        # whole group regardless of whether the leader already exited.
        _signal(signal.SIGTERM)
        try:
            proc.wait(timeout=max(0.05, float(grace)))
        except Exception:  # noqa: BLE001 - leader may already be gone
            pass
        deadline = time.monotonic() + max(0.05, float(grace))
        while time.monotonic() < deadline:
            try:
                os.killpg(int(owned_group), 0)
            except OSError:
                break
            time.sleep(0.02)
        _signal(signal.SIGKILL)
        try:
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 - reaped by the OS at worst
            pass
        return

    # No trusted owned group (synthetic/test process): only signal that object.
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001 - best effort
        pass
    try:
        proc.wait(timeout=max(0.05, float(grace)))
        return
    except Exception:  # noqa: BLE001 - escalate to kill
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 - best effort
        pass
    try:
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001 - reaped by the OS at worst
        pass


def _read_bounded(proc: Any, deadline: float, max_stdout: int,
                  max_stderr: int) -> tuple[bytes, bytes]:
    """Read both pipes until EOF or the hard deadline, with bounded buffers."""
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    streams = []
    try:
        for stream, label in ((proc.stdout, "out"), (proc.stderr, "err")):
            if stream is None:
                continue
            streams.append(stream)
            selector.register(stream, selectors.EVENT_READ, label)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TimedOut()
            events = selector.select(remaining)
            if not events:
                raise _TimedOut()
            for key, _ in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except (OSError, ValueError):
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(key.fileobj)
                    except (KeyError, ValueError):
                        pass
                    continue
                if key.data == "out":
                    stdout.extend(chunk)
                    if len(stdout) > max_stdout:
                        raise _OutputExceeded()
                else:
                    stderr.extend(chunk)
                    if len(stderr) > max_stderr:
                        del stderr[: len(stderr) - max_stderr]
        return bytes(stdout), bytes(stderr)
    finally:
        try:
            selector.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
        for stream in streams:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - best effort
                pass


def _run_worker_subprocess(
    payload: dict[str, Any],
    *,
    timeout: float,
    tmpdir: str,
    env: dict[str, str],
    worker_path: Path,
    python_executable: str,
) -> bytes:
    """Run exactly one owned worker process and return its bounded stdout."""
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise RenderUnavailable("native render request exceeded its limit")
    argv = [str(python_executable), str(worker_path)]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmpdir,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + max(0.1, float(timeout))
    max_stdout = (int(payload.get("max_output_bytes") or 1) * 4 // 3) + _WORKER_OUTPUT_OVERHEAD
    owned_pgid = None
    pid = getattr(proc, "pid", None)
    if pid:
        try:
            # start_new_session=True makes the new session/group id the worker
            # pid. Capture it now so cleanup still works after the leader exits.
            candidate = int(pid)
            if candidate != os.getpgrp():
                owned_pgid = candidate
        except (OSError, TypeError, ValueError):
            owned_pgid = None
    clean = False
    try:
        try:
            if proc.stdin is None:
                raise OSError("worker stdin unavailable")
            proc.stdin.write(encoded)
            proc.stdin.close()
        except (OSError, ValueError) as exc:
            raise RenderUnavailable("native render worker unavailable") from exc
        try:
            raw, _stderr = _read_bounded(proc, deadline, max_stdout, _MAX_STDERR_BYTES)
        except _TimedOut as exc:
            raise RenderUnavailable("native render timed out") from exc
        except _OutputExceeded as exc:
            raise RenderUnavailable("native render output exceeded its bound") from exc
        remaining = max(0.1, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise RenderUnavailable("native render timed out") from exc
        if proc.returncode != 0:
            # stderr is intentionally never surfaced: it may contain paths/env.
            raise RenderUnavailable(
                f"native render worker failed (exit {proc.returncode})")
        clean = True
        return raw
    finally:
        if clean:
            try:
                proc.wait(timeout=1)
            except Exception:  # noqa: BLE001 - leader already reaped
                pass
        else:
            # Timeout/crash: TERM/grace/KILL the WHOLE owned group even when the
            # leader already exited, so a stubborn descendant cannot survive.
            _terminate_owned(proc, pgid=owned_pgid, grace=1.0)
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:  # noqa: BLE001 - best effort
            pass


class NativePublicRenderer:
    """Bounded callable/object adapter for ONE public HTTPS page at a time.

    ``render(url, timeout=..., max_bytes=...)`` honours the caller's residual
    timeout (never spending a closure-wide budget when the source has less) and
    returns bounded rendered bytes or raises :class:`RenderUnavailable`.
    """

    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        *,
        worker_path: str | Path | None = None,
        python_executable: str | None = None,
        runner: Callable[..., bytes] | None = None,
        environ: dict[str, str] | None = None,
    ):
        self.policy = dict(policy or {})
        self.config = resolve_native_render_config(self.policy)
        self.worker_path = (Path(worker_path) if worker_path is not None
                            else Path(__file__).with_name("render_worker.py"))
        self.python_executable = str(python_executable or sys.executable)
        self._runner = runner or _run_worker_subprocess
        self._environ = dict(os.environ if environ is None else environ)

    def available(self) -> tuple[bool, str]:
        return native_renderer_availability(self.policy)

    @property
    def enabled(self) -> bool:
        return self.available()[0]

    def __call__(self, url: str, *, timeout: float | int | None = None,
                 max_bytes: int | None = None) -> bytes:
        """Callable form of :meth:`render` (bounded public URL in, bytes out)."""
        return self.render(url, timeout=timeout, max_bytes=max_bytes)

    def render(self, url: str, *, timeout: float | int | None = None,
               max_bytes: int | None = None) -> bytes:
        available, reason = self.available()
        if not available:
            raise RenderUnavailable(reason)
        try:
            vetted = pf.validate_public_url(url, generic_opt_in=True).geturl()
        except pf.URLRejected as exc:
            raise RenderUnavailable(f"render url rejected: {exc}") from exc
        cfg = self.config
        # An explicit residual timeout is authoritative: exhausted/nonpositive/
        # non-finite values fail closed BEFORE any tempdir or process is created.
        # The configured default applies only when no timeout was supplied.
        if timeout is None:
            budget = float(cfg["timeout_seconds"])
        else:
            try:
                requested = float(timeout)
            except (TypeError, ValueError) as exc:
                raise RenderUnavailable("invalid render timeout") from exc
            if not math.isfinite(requested) or requested <= 0:
                raise RenderUnavailable("render timeout budget exhausted")
            budget = min(requested, float(cfg["timeout_seconds"]))
        limit = _positive_int(max_bytes, cfg["max_output_bytes"])
        limit = min(limit, int(cfg["max_output_bytes"]))
        tmp_root = cfg["tmpdir"] or None
        tmpdir = tempfile.mkdtemp(prefix="proactive-render-", dir=tmp_root)
        env = _child_env(tmpdir, environ=self._environ)
        payload = {
            "url": vetted,
            "timeout": float(budget),
            "max_output_bytes": int(limit),
            "max_requests": int(cfg["max_requests"]),
            "max_network_bytes": int(cfg["max_network_bytes"]),
        }
        try:
            raw = self._runner(
                payload,
                timeout=float(budget),
                tmpdir=tmpdir,
                env=env,
                worker_path=self.worker_path,
                python_executable=self.python_executable,
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return _parse_worker_output(raw, limit)


__all__ = [
    "RenderUnavailable",
    "NativePublicRenderer",
    "build_native_renderer",
    "native_renderer_availability",
    "resolve_native_render_config",
]
