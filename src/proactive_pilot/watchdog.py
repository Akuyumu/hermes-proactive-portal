"""Whole-run and nested wall-clock containment.

A single scheduled run must never spin forever while holding the shared run
lock. This module installs restorable ``SIGALRM`` deadlines and -- crucially --
shares ONE registry across nested callers so a whole-run bound can never be
postponed or masked by an inner per-source bound.

Why a shared registry: ``collectors.wallclock_alarm`` wraps individual source
calls and ``run_deadline`` wraps the whole run. With independent timers the
inner context used to save the outer timer value and restore it WITHOUT
subtracting elapsed time, so every nested call pushed the outer deadline later;
a longer inner timer could also overwrite (mask) an earlier outer one. Here each
context registers an ABSOLUTE monotonic deadline, the single dispatcher handler
always arms the EARLIEST one, and on expiry it raises that owner's exception.

One-shot timer + nested scopes: ``SIGALRM`` is a one-shot timer, so when an
earlier inner deadline fires the dispatcher must (a) REMOVE that expired
deadline from the registry and (b) RE-ARM the timer to the next earliest
remaining deadline. Otherwise a caught inner ``SourceTimeout`` would consume the
single delivery and the still-active outer ``run_deadline`` would fall through
unfired. Removing the expired owner before raising also guarantees a scope can
never re-arm itself into a repeat fire / signal storm, and lets the *next*
deadline keep running.

Ambient lifecycle: the ambient ``SIGALRM`` handler and ``ITIMER_REAL`` interval
are captured only while the outermost scope is active and restored exactly when
it exits, tracked by an active-scope depth counter rather than by registry
emptiness. A deadline that already fired is removed from the registry while its
scope is still on the stack, so registry emptiness is not a reliable proxy for
"no scope active" -- the depth counter is.

Teardown: on the way out each scope re-checks the registry for an already-due
enclosing deadline (e.g. delivery was lost, or the outer expired while the inner
scope was still unwinding) and raises it immediately instead of letting the
enclosing teardown drop it. Setup/teardown mutate the shared registry inside a
``signal.pthread_sigmask`` critical section so the handler can never observe a
half-installed or half-removed state; stale/early timer deliveries that find no
due deadline are ignored (disarm/re-arm) rather than aborting the run, because
any genuinely active deadline is still registered and will be fired.

``RunDeadlineExceeded`` subclasses ``BaseException`` on purpose. Because the
deadline interrupts ``subprocess.run`` -- whose own handler kills and reaps the
child on any exception -- a hung child is terminated rather than leaked, and the
caller's ``StateStore.lock`` context manager still releases the advisory lock as
the exception propagates.

Off the main thread, or without ``SIGALRM``, it is a documented safe no-op and
the existing per-call/per-category budgets remain the primary bound. It never
sends, reads a source, or touches core Hermes.
"""
from __future__ import annotations

import dis
import math
import signal
import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator

# Re-arm an already-expired deadline as soon as possible rather than dropping it.
_MIN_ARM = 0.001
# If a per-frame opcode callback cannot run, force a synchronous abort rather
# than letting the original one-shot signal disappear in an infinite loop.
_TRACE_FALLBACK = 0.05


class RunDeadlineExceeded(BaseException):
    """The whole run exceeded its wall-clock bound and was aborted.

    Inherits ``BaseException`` on purpose: broad ``except Exception`` handlers
    must not convert a hung run into a benign per-source error and keep going.
    """


class _Deadline:
    __slots__ = ("at", "exc_factory", "name")

    def __init__(self, at: float, exc_factory: Callable[[], BaseException], name: str) -> None:
        self.at = at
        self.exc_factory = exc_factory
        self.name = name


_REGISTRY: list[_Deadline] = []
_LOCK = threading.RLock()
# Number of currently-active ``_deadline_scope`` contexts (main thread only).
# Ambient handler/timer lifecycle is tied to this, NOT to registry emptiness,
# because a fired deadline is removed from the registry while its scope is still
# on the stack.
_DEPTH = 0
# Ambient handler + ``ITIMER_REAL`` interval captured at the outermost scope so a
# surrounding timing contract is restored exactly, not merely cleared.
_AMBIENT: tuple[object, tuple[float, float]] | None = None
_AMBIENT_VALID = False


class _TraceDelivery:
    __slots__ = ("owner", "frame", "start", "end", "global_hook", "local_hook",
                 "lines", "opcodes", "active", "fired")

    def __init__(self, owner: _Deadline, frame: object, start: int, end: int) -> None:
        self.owner = owner
        self.frame = frame
        self.start = start
        self.end = end
        self.global_hook = sys.gettrace()
        self.local_hook = frame.f_trace
        self.lines = frame.f_trace_lines
        self.opcodes = frame.f_trace_opcodes
        self.active = False
        self.fired = False


# Only the main thread has signal scopes. At most one pending safe delivery is
# active; completed states remain until their owner scope exits so an existing
# debugger hook can be recovered if CPython disables it on trace exceptions.
_ACTIVE_TRACE: _TraceDelivery | None = None
_TRACE_STATES: list[_TraceDelivery] = []


def _trace_other_frames(frame: object, event: str, arg: object) -> None:  # noqa: ARG001
    """Enable tracing for one interrupted frame without tracing new frames."""
    return None


def _stop_trace(state: _TraceDelivery) -> None:
    global _ACTIVE_TRACE
    if not state.active:
        return
    state.active = False
    if _ACTIVE_TRACE is state:
        _ACTIVE_TRACE = None
    state.frame.f_trace = state.local_hook
    state.frame.f_trace_lines = state.lines
    state.frame.f_trace_opcodes = state.opcodes
    if state.global_hook is None and sys.gettrace() is _trace_other_frames:
        sys.settrace(None)


def _finish_trace_owner(owner: _Deadline) -> None:
    for state in tuple(_TRACE_STATES):
        if state.owner is not owner:
            continue
        _stop_trace(state)
        # A raised exception from a trace callback makes CPython unset tracing.
        # Restore pre-existing hooks on every scope-exit path, including when
        # a source timeout is caught by an enclosing run scope.
        if state.fired and state.global_hook is not None and sys.gettrace() is None:
            sys.settrace(state.global_hook)
        if state.fired and state.local_hook is not None and state.frame.f_trace is None:
            state.frame.f_trace = state.local_hook
            state.frame.f_trace_lines = state.lines
            state.frame.f_trace_opcodes = state.opcodes
        _TRACE_STATES.remove(state)


def _deliver_at_protected_opcode(frame: object, event: str, arg: object) -> object:  # noqa: ARG001
    state = _ACTIVE_TRACE
    if state is None or frame is not state.frame:
        return None
    try:
        prior = state.local_hook
        if prior is not None and (event != "opcode" or state.opcodes):
            state.local_hook = prior(frame, event, arg)
        if event == "opcode" and state.start <= frame.f_lasti < state.end:
            with _LOCK:
                winner = _pop_due_locked(time.monotonic())
                _rearm_locked()
            _stop_trace(state)
            if winner is not None:
                state.fired = True
                raise winner.exc_factory()
            return state.local_hook
    except BaseException:
        _stop_trace(state)
        raise
    return _deliver_at_protected_opcode


def _arm_protected_delivery(owner: _Deadline, frame: object, interval: tuple[int, int]) -> None:
    global _ACTIVE_TRACE
    state = _TraceDelivery(owner, frame, *interval)
    _TRACE_STATES.append(state)
    try:
        # sys.settrace is per-thread. With no existing global tracer a stub
        # enables only this frame's opcode events and returns None elsewhere.
        # An existing global tracer remains installed and is not replaced.
        if state.global_hook is None:
            sys.settrace(_trace_other_frames)
        frame.f_trace = _deliver_at_protected_opcode
        frame.f_trace_lines = state.lines
        frame.f_trace_opcodes = True
        state.active = True
        _ACTIVE_TRACE = state
        signal.setitimer(signal.ITIMER_REAL, _TRACE_FALLBACK)
    except BaseException:
        _finish_trace_owner(owner)
        raise


def _signal_capable() -> bool:
    return hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()


def _block_sigalrm() -> object | None:
    """Block SIGALRM for a tiny setup/teardown critical section (Linux main thread).

    Returns the previous mask (or ``None`` when unsupported), to be passed back
    to :func:`_restore_sigmask`. Blocking prevents the handler from observing a
    half-mutated registry or re-entering teardown mid-unwind.
    """
    if not hasattr(signal, "pthread_sigmask"):
        return None
    try:
        return signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    except (ValueError, OSError, AttributeError):
        return None


def _restore_sigmask(previous: object | None) -> None:
    if previous is None or not hasattr(signal, "pthread_sigmask"):
        return
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    except (ValueError, OSError, AttributeError):
        pass


def _drain_pending_sigalrm() -> None:
    """Consume a stale/queued SIGALRM while it is still blocked.

    Prevents an already-generated delivery from firing the (possibly restored)
    ambient handler -- which may be ``SIG_DFL`` -- after we have decided there is
    nothing left to enforce.
    """
    if not hasattr(signal, "sigtimedwait"):
        return
    try:
        if signal.SIGALRM not in signal.sigpending():
            return
        signal.sigtimedwait({signal.SIGALRM}, 0)
    except (ValueError, OSError, AttributeError):
        pass


def _validate_finite(seconds: float) -> float:
    try:
        limit = float(seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("deadline seconds must be a real number") from exc
    if not math.isfinite(limit):
        # Never let a NaN/inf deadline silently disable the safety bound.
        raise ValueError("deadline seconds must be finite")
    return limit


def _pop_due_locked(now: float) -> _Deadline | None:
    """Remove and return the earliest already-expired deadline, if any."""
    due = [deadline for deadline in _REGISTRY if deadline.at <= now + 1e-9]
    if not due:
        return None
    winner = min(due, key=lambda d: d.at)
    try:
        _REGISTRY.remove(winner)
    except ValueError:  # pragma: no cover - defensive: already removed
        pass
    return winner


def _rearm_locked() -> None:
    if not _REGISTRY:
        signal.setitimer(signal.ITIMER_REAL, 0)
        return
    earliest = min(_REGISTRY, key=lambda d: d.at)
    remaining = earliest.at - time.monotonic()
    signal.setitimer(signal.ITIMER_REAL, remaining if remaining > 0 else _MIN_ARM)


def _unprotected_loop_edge(frame: object) -> tuple[int, int] | None:
    """Is a signal being dispatched at a backward jump just past a try range?

    CPython checks for signals at backward jumps. A loop's jump can be *outside*
    the lexical try's exception-table interval even though its preceding loop
    condition and its destination are inside. Raising at that jump bypasses the
    intended except clause. Only this verifiable boundary is retried; in every
    other frame we raise synchronously, including during blocking C calls.
    """
    if frame is None:
        return None
    code = frame.f_code
    offset = frame.f_lasti
    if offset < 0 or offset >= len(code.co_code) or dis.opname[code.co_code[offset]] != "JUMP_BACKWARD":
        return None
    instructions = list(dis.get_instructions(code))
    for index, instruction in enumerate(instructions):
        if instruction.offset != offset or instruction.opname != "JUMP_BACKWARD" or index == 0:
            continue
        previous = instructions[index - 1].offset
        target = instruction.argval
        for entry in dis.Bytecode(code).exception_entries:
            if (entry.start <= previous < entry.end
                    and entry.start <= target < entry.end
                    and not entry.start <= offset < entry.end):
                return entry.start, entry.end
    return None


def _dispatcher(signum: int, frame: object) -> None:  # noqa: ANN001, ARG001
    with _LOCK:
        now = time.monotonic()
        due = [deadline for deadline in _REGISTRY if deadline.at <= now + 1e-9]
        if due and _ACTIVE_TRACE is None:
            protected = _unprotected_loop_edge(frame)
            if protected is not None:
                # Signals are polled at the backward jump *outside* the try's
                # exception table. A call-free loop may never hit a different
                # signal polling point, so a timer retry alone cannot help.
                # Trace this frame only until its next protected opcode.
                _arm_protected_delivery(min(due, key=lambda d: d.at), frame, protected)
                return
        elif _ACTIVE_TRACE is not None:
            # A pathological tracer that cannot run must not make the deadline
            # unbounded: the short fallback delivers synchronously.
            _stop_trace(_ACTIVE_TRACE)
        winner = _pop_due_locked(now)
        # Always re-arm for the next earliest deadline: the fired one was just
        # removed, and a still-active enclosing deadline must keep running.
        _rearm_locked()
    if winner is None:
        # A stale/early delivery with no due deadline: the timer was re-armed
        # above (or disarmed when no deadline remains). Never abort a run for a
        # signal that enforces nothing.
        return
    raise winner.exc_factory()


@contextmanager
def _deadline_scope(limit: float, exc_factory: Callable[[], BaseException], name: str) -> Iterator[None]:
    if not _signal_capable():
        yield
        return

    global _AMBIENT, _AMBIENT_VALID, _DEPTH
    entry = _Deadline(time.monotonic() + limit, exc_factory, name)

    mask = _block_sigalrm()
    try:
        with _LOCK:
            if _DEPTH == 0:
                prior_handler = signal.getsignal(signal.SIGALRM)
                prior_timer = signal.getitimer(signal.ITIMER_REAL)
                signal.signal(signal.SIGALRM, _dispatcher)
                _AMBIENT = (prior_handler, prior_timer)
                _AMBIENT_VALID = True
            _DEPTH += 1
            _REGISTRY.append(entry)
            _rearm_locked()
    finally:
        _restore_sigmask(mask)

    pending = False
    fire: _Deadline | None = None
    try:
        yield
    except BaseException:
        pending = True
        raise
    finally:
        mask = _block_sigalrm()
        try:
            with _LOCK:
                _finish_trace_owner(entry)
                # A due signal deferred at a loop edge (or lost outright) must
                # still fire if this scope exits before the next delivery.
                if not pending:
                    fire = _pop_due_locked(time.monotonic())
                try:
                    _REGISTRY.remove(entry)
                except ValueError:
                    pass  # already removed by the dispatcher when it fired
                _DEPTH -= 1
                if _DEPTH <= 0:
                    _DEPTH = 0
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    if _AMBIENT_VALID:
                        handler, interval = _AMBIENT
                        try:
                            signal.signal(signal.SIGALRM, handler)
                        except (ValueError, TypeError):
                            pass
                        value, every = interval
                        if value or every:
                            try:
                                signal.setitimer(signal.ITIMER_REAL, value, every)
                            except (ValueError, TypeError):
                                pass
                        _AMBIENT_VALID = False
                else:
                    _rearm_locked()
                if fire is not None or _DEPTH == 0:
                    _drain_pending_sigalrm()
        finally:
            _restore_sigmask(mask)
        if fire is not None:
            raise fire.exc_factory()


@contextmanager
def run_deadline(seconds: float) -> Iterator[None]:
    """Bound the enclosed whole run by ``seconds`` (main thread only).

    ``seconds <= 0`` is a documented no-op (bound disabled). A non-finite value
    is rejected loudly so a corrupt policy can never silently disable the bound.
    """
    limit = _validate_finite(0.0 if not seconds else seconds)
    if limit <= 0:
        yield
        return
    with _deadline_scope(limit, lambda: RunDeadlineExceeded("run exceeded wall-clock bound"), "run"):
        yield


@contextmanager
def deadline_guard(
    seconds: float,
    exc_factory: Callable[[], BaseException],
    *,
    minimum: float = 0.05,
) -> Iterator[None]:
    """Nest a restorable per-source deadline of ``seconds`` (floor ``minimum``).

    Participates in the shared registry so an earlier outer deadline is honoured
    (and its exception type raised) instead of being postponed or masked.
    """
    limit = _validate_finite(seconds)
    if limit < minimum:
        limit = minimum
    with _deadline_scope(limit, exc_factory, "source"):
        yield
