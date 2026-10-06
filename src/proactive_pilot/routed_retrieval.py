"""V0.4.0 routed public retrieval: capability-honest, consent-gated fallback.

This module owns the ONE place the ordinary public collector may reach a
rendering/extraction fallback after the direct DNS-pinned transport reports a
JavaScript shell (a 200 anti-bot/dynamic skeleton).

Hard boundaries (all fail closed):

* Direct pinned HTTPS transport stays the default and is the only transport that
  runs with no explicit consent. Fallbacks never replace it and never relax an
  existing SSRF/credential/redirect/secret-query check.
* A fallback is attempted ONLY for a ``dynamic`` JS-shell classification. A
  ``blocked`` challenge/interstitial/paywall/login page is terminal: it is never
  rendered, never bypassed, never treated as evidence or as a healthy empty
  result.
* Two fallback surfaces exist and BOTH are off by default and gated behind an
  explicit per-policy consent flag:
    - an injected, approved native renderer callable (public URL in, bounded
      bytes out). The generic shared Hermes browser tool is deliberately NOT
      reused: it carries the owner's session/cookies and is not a constrained
      public-only adapter.
    - an optional Firecrawl adapter driven through the EXACT supported native
      plugin contract ``ctx.call_mcp(server, tool, arguments, timeout)``. The
      host owns the OAuth connection and the server allowlist; this plugin never
      embeds an endpoint, an API key, or an OAuth client. The server and tool are
      named by configuration, never guessed, and only ONE simple public scrape
      argument shape is ever sent.

Firecrawl metered budget (owner quota priority): every provider call is a
reserved, persisted local attempt. A local attempt ceiling is enforced per cycle
AND per UTC day, atomically persisted across collectors and restarts, with a
provider 429 cooldown honoured. The ledger is deliberately honest: a failed
provider attempt still consumes the reserved local attempt. Local attempt counts
are NOT provider billing credits.

The renderer/MCP call is handed the public URL alone: no cookies, auth headers,
private context, or raw source material is ever forwarded.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import publicfeeds as pf

# The verified native host secret surface (kept for the explicit Firecrawl KEY
# route; optional and never required now that the host owns OAuth through
# ``ctx.call_mcp``). Imported lazily so the staging plugin has no hard dependency
# on the host runtime and tests never touch it.
NATIVE_SECRET_MODULE = "agent.secret_scope"
NATIVE_SECRET_FUNCTION = "get_secret"
FIRECRAWL_SECRET_NAME = "FIRECRAWL_API_KEY"

# Persisted metered-attempt ledger. Never stores response bodies; only local
# attempt counters, a provider cooldown deadline and per-URL freshness markers.
METERED_STATE_FILE = "metered_retrieval.json"
METERED_SCHEMA = 1

# Shared cross-process advisory locks (same fcntl.flock practice as StateStore).
# * ``METERED_LOCK_FILE`` serialises the whole read/rollover/check/reserve/save
#   transaction (and cooldown/cache mutations) so a lost update is impossible.
# * ``METERED_PROVIDER_LOCK_FILE`` is a SEPARATE non-blocking gate held across the
#   provider network call at conservative concurrency 1. Keeping it separate means
#   the ledger transaction lock is never held across the network (no deadlock).
METERED_LOCK_FILE = "metered_retrieval.lock"
METERED_PROVIDER_LOCK_FILE = "metered_retrieval.provider.lock"


@contextmanager
def _transaction_lock(store: Any) -> Iterator[bool]:
    """Blocking exclusive cross-process lock for one ledger transaction.

    Mirrors ``state.file_lock``/``StateStore.lock`` (owner-only 0600 file under the
    private state root, ``fcntl.flock``) but BLOCKS rather than failing so
    concurrent reservations serialise instead of losing updates. Without a store
    root there is no shared artifact, so the process-local ``RLock`` suffices and
    this yields True.
    """
    root = getattr(store, "root", None)
    if root is None:
        yield True
        return
    lock_path = Path(root) / METERED_LOCK_FILE
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        # Cannot take the shared lock: fail closed rather than risk a lost update.
        yield False
        return
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            acquired = True
        except OSError as exc:  # pragma: no cover - defensive
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)

# Firecrawl's documented simple scrape shape (URL + markdown only). The argument
# KEY NAMES are configurable because the exact remote tool schema is owned and
# discovered by the host; when unknown we expose that precisely and send nothing
# but this documented shape.
_FIRECRAWL_DEFAULT_URL_ARG = "url"
_FIRECRAWL_DEFAULT_FORMATS_ARG = "formats"
_FIRECRAWL_FORMATS = ("markdown",)

_429_MARKERS = (
    "429", "too many requests", "rate limit", "rate-limit", "ratelimit",
    "rate_limit_exceeded", "quota exceeded", "quota_exceeded",
)
_RETRY_AFTER_RE = re.compile(
    r"retry[\s_-]?after[\"'\s:=]*(\d{1,6})\s*(?:s|sec|seconds)?", re.IGNORECASE)


class RenderUnavailable(RuntimeError):
    """No approved native render/extract route is available (candid refusal)."""


class RetrievalUnavailable(RuntimeError):
    """No metered fallback attempt may run (candid refusal; never a fake result)."""


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _optional_nonnegative_int(value: Any, default: int | None = None) -> int | None:
    """An optional non-negative int; ``None``/empty leaves it unset (omit the field).

    Used for the verified Firecrawl ``maxAge`` freshness knob: when the owner
    (or a benchmark policy) does not set it, the argument is NOT sent, so the
    documented simple scrape shape is unchanged.
    """
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def resolve_native_secret(name: str, ctx: Any = None) -> str:
    """Resolve a credential through the verified native host surface only.

    Kept for the explicit key route. Preference order: the injected host plugin
    context accessor (``ctx.get_secret``), then the lazily imported native secret
    scope (``agent.secret_scope.get_secret``). Any failure (absent module, unbound
    multiplex scope, raised accessor) yields ``""``: the caller must treat that as
    "unavailable", never fall back to reading a secret file itself.
    """
    key = str(name or "")
    if not key:
        return ""
    getter = getattr(ctx, "get_secret", None) if ctx is not None else None
    if callable(getter):
        try:
            value = getter(key)
            if value:
                return str(value)
        except Exception:  # noqa: BLE001 - an unavailable scope is "no secret"
            return ""
    try:
        import importlib

        module = importlib.import_module(NATIVE_SECRET_MODULE)
        function = getattr(module, NATIVE_SECRET_FUNCTION)
        value = function(key)
        return str(value) if value else ""
    except Exception:  # noqa: BLE001 - host surface absent/unbound => unavailable
        return ""


def _has_call_mcp(ctx: Any) -> bool:
    """True when the host-built context exposes the supported ``ctx.call_mcp``."""
    if ctx is None:
        return False
    try:
        getter = getattr(ctx, "call_mcp", None)
    except Exception:  # noqa: BLE001 - a property error means "unavailable"
        return False
    return callable(getter)


def resolve_firecrawl_config(policy: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise the typed retrieval config; neutral and empty by default.

    No server or tool name is ever defaulted/guessed. The exact argument key
    names remain configurable because the remote tool schema is host-owned; when
    the schema has not been verified this stays explicit (``schema_verified`` is
    False) rather than being fabricated.
    """
    p = policy or {}
    return {
        "enabled": bool(p.get("firecrawl_enabled", False)),
        "consent": bool(p.get("firecrawl_consent", False)),
        "server": str(p.get("firecrawl_mcp_server") or "").strip(),
        "tool": str(p.get("firecrawl_mcp_tool") or "").strip(),
        "url_arg": str(p.get("firecrawl_mcp_url_arg") or _FIRECRAWL_DEFAULT_URL_ARG).strip()
        or _FIRECRAWL_DEFAULT_URL_ARG,
        "formats_arg": str(p.get("firecrawl_mcp_formats_arg") or _FIRECRAWL_DEFAULT_FORMATS_ARG).strip()
        or _FIRECRAWL_DEFAULT_FORMATS_ARG,
        "max_attempts_per_cycle": _positive_int(p.get("firecrawl_max_attempts_per_cycle"), 1),
        "max_attempts_per_day": _positive_int(p.get("firecrawl_max_attempts_per_day"), 10),
        "concurrency": _positive_int(p.get("firecrawl_concurrency"), 1),
        "timeout_seconds": _positive_int(p.get("firecrawl_timeout_seconds"), 30),
        "cooldown_minutes": _nonnegative_int(p.get("firecrawl_cooldown_minutes"), 60),
        "cache_ttl_minutes": _nonnegative_int(p.get("firecrawl_cache_ttl_minutes"), 60),
        # Verified-schema freshness knob. ``None`` (default) omits the field entirely;
        # a bounded integer (0 = live fetch) is sent as ``maxAge`` when the owner has
        # a verified schema. Never a premium/stealth/profile option.
        "max_age": _optional_nonnegative_int(p.get("firecrawl_mcp_max_age")),
        "schema_verified": bool(p.get("firecrawl_schema_verified", False)),
    }


def firecrawl_route(
    policy: dict[str, Any] | None,
    *,
    ctx: Any = None,
    client: Any = None,
) -> tuple[bool, str]:
    """``(available, reason)`` for the optional Firecrawl MCP adapter.

    Off unless the owner set BOTH ``firecrawl_enabled`` and ``firecrawl_consent``,
    a server AND tool are named by configuration, and the host-built context
    exposes the supported ``ctx.call_mcp`` (an injected legacy client is also
    accepted). The key is never required, returned, copied, or logged: the host
    owns the OAuth connection.
    """
    cfg = resolve_firecrawl_config(policy)
    if not cfg["enabled"]:
        return False, "firecrawl disabled by policy"
    if not cfg["consent"]:
        return False, "firecrawl requires explicit owner consent"
    if not cfg["server"] or not cfg["tool"]:
        return False, ("firecrawl MCP server/tool not configured (remote schema is "
                       "host-owned and is not guessed)")
    if client is not None or _has_call_mcp(ctx):
        if not cfg["schema_verified"]:
            return True, ("native ctx.call_mcp; remote schema unverified, sending only the "
                          "documented simple scrape {url, formats:[markdown]}")
        return True, "native ctx.call_mcp (verified simple scrape schema)"
    return False, ("no native ctx.call_mcp available (host owns the OAuth connection; "
                   "no API-key duplication)")


def native_render_route(
    policy: dict[str, Any] | None,
    *,
    renderer: Callable[[str], Any] | None = None,
) -> tuple[bool, str]:
    """``(available, reason)`` for the bounded native render fallback.

    Off unless the owner set BOTH ``native_render_fallback_enabled`` and
    ``native_render_consent`` AND an approved renderer callable was injected by
    the host. When none is injected the generic shared browser tool is NOT
    substituted: this is an explicit interface blocker, reported candidly.
    """
    policy = policy or {}
    if not bool(policy.get("native_render_fallback_enabled", False)):
        return False, "native render fallback disabled by policy"
    if not bool(policy.get("native_render_consent", False)):
        return False, "native render fallback requires explicit owner consent"
    if renderer is None:
        return False, ("no approved constrained native renderer injected; the generic "
                       "shared browser tool is not reused (session/cookie/SSRF risk)")
    return True, "injected approved native renderer"


def capabilities(
    policy: dict[str, Any] | None,
    *,
    ctx: Any = None,
    renderer: Callable[[str], Any] | None = None,
    firecrawl_client: Any = None,
) -> dict[str, Any]:
    """Candid capability report for status/doctor. No secret value is included."""
    render_ok, render_reason = native_render_route(policy, renderer=renderer)
    fc_ok, fc_reason = firecrawl_route(policy, ctx=ctx, client=firecrawl_client)
    cfg = resolve_firecrawl_config(policy)
    return {
        "direct_pinned_https": {"available": True, "reason": "default DNS-pinned public transport"},
        "native_render_fallback": {"available": render_ok, "reason": render_reason},
        "firecrawl": {
            "available": fc_ok,
            "reason": fc_reason,
            "server_configured": bool(cfg["server"]),
            "tool_configured": bool(cfg["tool"]),
            # The remote tool schema is host-owned until a verified reference is
            # supplied; this is the precise unknown, never a fabricated contract.
            "schema_verified": cfg["schema_verified"],
        },
        # Archive/paid routes are never claimed: only direct live fetch is the baseline.
        "provenance": "live",
    }


def render_js_shell(
    url: str,
    *,
    renderer: Callable[[str], Any] | None,
    policy: dict[str, Any] | None = None,
    timeout: int | None = None,
    max_bytes: int | None = None,
) -> bytes:
    """Render ONE public JS-shell URL through the injected approved renderer.

    Fails closed (raises :class:`RenderUnavailable`) when no approved renderer is
    available, the URL is not a vetted public HTTPS URL, the renderer raises, or
    the renderer returns nothing. The URL is re-validated with the same public
    rules the direct transport uses before the renderer is called; only the URL is
    forwarded (no cookies/auth/private context).
    """
    policy = policy or {}
    available, reason = native_render_route(policy, renderer=renderer)
    if not available:
        raise RenderUnavailable(reason)
    try:
        vetted = pf.validate_public_url(url, generic_opt_in=True).geturl()
    except pf.URLRejected as exc:
        raise RenderUnavailable(f"render url rejected: {exc}") from exc
    limit = int(max_bytes if max_bytes is not None else policy.get("retrieval_fallback_max_bytes", pf.DEFAULT_MAX_BYTES))
    try:
        rendered = renderer(vetted)
    except Exception as exc:  # noqa: BLE001 - a failed/side-effecting renderer is unavailable
        raise RenderUnavailable(f"native render failed ({type(exc).__name__})") from exc
    if not rendered:
        raise RenderUnavailable("native renderer returned no content")
    data = rendered if isinstance(rendered, (bytes, bytearray)) else str(rendered).encode("utf-8", "replace")
    return bytes(data[: max(1, limit)])


# --------------------------------------------------------------------------- #
# Metered attempt ledger (persistent, atomic, shared)
# --------------------------------------------------------------------------- #


def _url_key(url: str) -> str:
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()[:16]


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _utc_day(now: datetime | None) -> str:
    current = now or _utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).date().isoformat()


# Maximum per-URL freshness markers a persisted ledger may carry (mark_scraped
# prunes to this bound); a persisted map larger than this is malformed.
_SCRAPED_MAX_MARKERS = 256


def _is_plain_int(value: Any) -> bool:
    """True for a real ``int`` but never a ``bool`` (which is an ``int``)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_ledger_day(value: Any) -> bool:
    """A persisted UTC day must be a canonical ``YYYY-MM-DD`` date string."""
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    return parsed.strftime("%Y-%m-%d") == value


def _parse_aware_iso(value: Any) -> datetime | None:
    """Parse an ISO timestamp that MUST carry a timezone offset (or ``Z``)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed


def _validate_persisted_ledger(data: Any) -> dict[str, Any] | None:
    """Strictly validate a loaded ledger; ``None`` means fail closed.

    Every admission-relevant persisted field is checked against its exact
    expected type/shape. Invalid values are NEVER coerced to a benign default
    (that would silently re-open a quota): the caller marks the ledger corrupt
    and refuses all metered activity, leaving the on-disk bytes untouched.

    A cleanly absent file never reaches here: the store returns the valid blank
    default, which passes. An EXISTING ledger missing any required field is
    malformed and fails closed.
    """
    if not isinstance(data, dict):
        return None
    schema = data.get("schema")
    if not _is_plain_int(schema) or schema != METERED_SCHEMA:
        return None
    # Required admission-relevant fields must be present (an existing partial
    # ledger must not be silently completed).
    for field in ("day", "day_used", "cooldown_until", "scraped"):
        if field not in data:
            return None
    day = data.get("day")
    if not _valid_ledger_day(day):
        return None
    day_used = data.get("day_used")
    if not _is_plain_int(day_used) or day_used < 0:
        return None
    cooldown = data.get("cooldown_until")
    if cooldown != "" and _parse_aware_iso(cooldown) is None:
        return None
    scraped = data.get("scraped")
    if not isinstance(scraped, dict) or len(scraped) > _SCRAPED_MAX_MARKERS:
        return None
    for key, value in scraped.items():
        if not isinstance(key, str) or _parse_dt(value) is None:
            return None
    return {
        "schema": METERED_SCHEMA,
        "day": day,
        "day_used": day_used,
        "cooldown_until": cooldown,
        "scraped": dict(scraped),
    }


class MeteredAttemptLedger:
    """A persisted, atomic local-attempt ledger for a quota-bearing route.

    Guarantees:
    * Every reserve persists BEFORE the provider call, so a crashed/restarted
      collector still counts the attempt (reserve-before-call).
    * The read/rollover/check/reserve/save transaction runs under a SHARED
      cross-process advisory file lock (same ``fcntl.flock`` practice as
      :class:`~proactive_pilot.state.StateStore`), so ``os.replace`` alone cannot
      lose a concurrent update: simultaneous reservations at the ceiling admit
      exactly the remaining slots.
    * Cooldown and per-URL cache mutations take the same shared lock.
    * The UTC-day ceiling is reloaded from disk on every reserve, so it is shared
      across processes and survives restarts.
    * Unrelated configuration changes never reset the counter; only the UTC day
      rollover does.
    * A provider 429 stamps a cooldown deadline that is honoured on later cycles.
    * Global provider concurrency is bounded across processes by a SEPARATE
      non-blocking provider-operation lock held across the provider call at
      conservative concurrency 1; the transaction lock is never held across the
      network. The lock is released by the OS on crash.
    * A per-cycle ceiling is enforced by the (short-lived) ledger instance that is
      shared by every collector within the same run.
    * A corrupt persisted ledger fails closed (denies reservations) instead of
      silently resetting the quota.
    """

    def __init__(
        self,
        store: Any | None = None,
        *,
        now: datetime | None = None,
        per_cycle: int = 1,
        per_day: int = 10,
        concurrency: int = 1,
        cooldown_minutes: int = 60,
        cache_ttl_minutes: int = 0,
        fingerprint: str = "",
    ):
        self._store = store
        self._per_cycle = max(0, int(per_cycle))
        self._per_day = max(0, int(per_day))
        self._cooldown_minutes = max(0, int(cooldown_minutes))
        self._cache_ttl_minutes = max(0, int(cache_ttl_minutes))
        self._fingerprint = str(fingerprint or "")
        self._cycle_used = 0
        self._day = _utc_day(now)
        self._lock = threading.RLock()
        # Global provider gate: conservative concurrency 1 held ACROSS the provider
        # call via a separate non-blocking cross-process file lock (never held with
        # the ledger transaction lock, so no lock is held across the network while
        # another process waits to read the ledger).
        self._provider_fd: int | None = None
        self._holding = False
        self._corrupt = False
        # Sticky fail-closed marker: once a lock/persistence failure is seen the
        # instance denies all further metered activity instead of silently
        # continuing on possibly-lost ledger state.
        self._failed_reason = ""
        self._state = self._load()

    # -- persistence -----------------------------------------------------
    def _blank(self) -> dict[str, Any]:
        return {
            "schema": METERED_SCHEMA,
            "day": self._day,
            "day_used": 0,
            "cooldown_until": "",
            "scraped": {},
            "config_fingerprint": self._fingerprint,
        }

    def _load(self) -> dict[str, Any]:
        self._corrupt = False
        if self._store is None:
            return self._blank()
        try:
            # Strict load: a corrupt ledger must FAIL CLOSED, never silently reset
            # the daily counter (which would re-open the quota). Production
            # StateStore supports ``strict=True``; there is deliberately NO
            # non-strict compatibility fallback, because that would silently
            # reset a corrupt/unreadable store (a TypeError from an incompatible
            # store is treated as corruption and fails closed too).
            data = self._store.load(METERED_STATE_FILE, self._blank(), strict=True)
        except Exception:  # noqa: BLE001 - unreadable/undecodable state fails closed
            self._corrupt = True
            return self._blank()
        validated = _validate_persisted_ledger(data)
        if validated is None:
            # A matching-schema ledger carrying any malformed/half-written
            # admission field is corrupt. Fail closed and NEVER rewrite it to a
            # benign 0 (that would silently re-open the daily quota). The bytes
            # on disk are left byte-identical because every mutation checks
            # ``self._corrupt`` before saving.
            self._corrupt = True
            return self._blank()
        validated["config_fingerprint"] = self._fingerprint
        return validated

    def _fail_closed(self, reason: str) -> str:
        """Record a sticky fail-closed reason and return it.

        A lock-open/persistence failure must not be a one-off: the instance is on
        possibly-lost ledger state, so every later operation denies rather than
        risk an uncounted or over-quota provider call.
        """
        if not self._failed_reason:
            self._failed_reason = str(reason)
        return self._failed_reason

    def unavailable_reason(self) -> str:
        """The fail-closed reason for this instance, or ``""`` when healthy."""
        if self._failed_reason:
            return self._failed_reason
        if self._corrupt:
            return "metered ledger corrupt (failing closed)"
        return ""

    def _save(self) -> None:
        if self._store is None:
            return
        # Atomic temp-file + os.replace via StateStore; never a partial write.
        self._store.save(METERED_STATE_FILE, dict(self._state))

    def _roll_day(self, today: str) -> None:
        if self._state.get("day") != today:
            self._state["day"] = today
            self._state["day_used"] = 0
            # A new UTC day clears stale per-URL markers too.
            self._state["scraped"] = {}

    # -- provider gate (separate from the ledger transaction lock) --------
    def _acquire_provider(self) -> bool:
        """Take the global provider-operation gate (non-blocking, concurrency 1)."""
        if self._provider_fd is not None:
            return False
        root = getattr(self._store, "root", None)
        if root is None:
            # No shared artifact root: the in-process holder flag still bounds this
            # ledger instance (there is no cross-process state to coordinate).
            self._provider_fd = -1
            return True
        lock_path = Path(root) / METERED_PROVIDER_LOCK_FILE
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        self._provider_fd = fd
        return True

    def _release_provider(self) -> None:
        fd = self._provider_fd
        if fd is None:
            return
        self._provider_fd = None
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - defensive
                pass
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - defensive
                pass

    # -- quota -----------------------------------------------------------
    def reserve(self, now: datetime | None = None) -> tuple[bool, str]:
        """Reserve ONE local attempt, persisting before the provider call.

        The full read/rollover/check/reserve/save transaction runs under a shared
        cross-process advisory lock, so simultaneous reservations cannot lose an
        update. The provider gate is taken separately and held across the network
        call by :meth:`release`; this transaction lock is never held across it.
        """
        current = now or _utcnow()
        today = _utc_day(current)
        with self._lock:
            if self._failed_reason:
                return False, self._failed_reason
            with _transaction_lock(self._store) as locked:
                if not locked:
                    return False, self._fail_closed(
                        "metered ledger lock unavailable (failing closed)")
                self._state = self._load()
                if self._corrupt:
                    return False, "metered ledger corrupt (failing closed)"
                self._roll_day(today)
                cooldown = _parse_dt(self._state.get("cooldown_until"))
                if cooldown is not None and cooldown > current:
                    return False, f"provider 429 cooldown until {cooldown.isoformat()}"
                if self._cycle_used >= self._per_cycle:
                    return False, f"per-cycle metered attempt ceiling ({self._per_cycle})"
                if int(self._state.get("day_used", 0)) >= self._per_day:
                    return False, f"daily metered attempt ceiling ({self._per_day})"
                if not self._acquire_provider():
                    return False, "metered concurrency ceiling reached"
                self._holding = True
                # Persist the reservation BEFORE the network call.
                self._state["day_used"] = int(self._state.get("day_used", 0)) + 1
                self._cycle_used += 1
                try:
                    self._save()
                except Exception:  # noqa: BLE001 - cannot persist => never call
                    self._holding = False
                    self._release_provider()
                    return False, self._fail_closed(
                        "metered ledger persistence failed (failing closed)")
                return True, "reserved"

    def release(self) -> None:
        with self._lock:
            if self._holding:
                self._holding = False
                self._release_provider()

    def record_429(self, now: datetime | None = None, *, retry_after_seconds: int | None = None) -> str:
        """Stamp a provider cooldown from a 429 (honouring Retry-After).

        Fails closed (raising :class:`RetrievalUnavailable`) when the shared lock
        cannot be taken, the ledger is corrupt, or the cooldown cannot be
        persisted: the instance then denies all further metered attempts rather
        than silently continuing without a cooldown.
        """
        current = now or _utcnow()
        with self._lock:
            if self._failed_reason:
                raise RetrievalUnavailable(self._failed_reason)
            with _transaction_lock(self._store) as locked:
                if not locked:
                    raise RetrievalUnavailable(self._fail_closed(
                        "metered ledger lock unavailable (failing closed)"))
                self._state = self._load()
                if self._corrupt:
                    raise RetrievalUnavailable("metered ledger corrupt (failing closed)")
                seconds = max(self._cooldown_minutes * 60, _nonnegative_int(retry_after_seconds, 0))
                until = current + timedelta(seconds=seconds)
                self._state["cooldown_until"] = until.isoformat()
                try:
                    self._save()
                except Exception:  # noqa: BLE001 - cannot persist => fail closed
                    raise RetrievalUnavailable(self._fail_closed(
                        "metered ledger persistence failed (failing closed)"))
                return until.isoformat()

    def record_success(self) -> None:
        with self._lock:
            if self._failed_reason:
                raise RetrievalUnavailable(self._failed_reason)
            with _transaction_lock(self._store) as locked:
                if not locked:
                    raise RetrievalUnavailable(self._fail_closed(
                        "metered ledger lock unavailable (failing closed)"))
                self._state = self._load()
                if self._corrupt:
                    raise RetrievalUnavailable("metered ledger corrupt (failing closed)")
                if self._state.get("cooldown_until"):
                    self._state["cooldown_until"] = ""
                    try:
                        self._save()
                    except Exception:  # noqa: BLE001 - cannot persist => fail closed
                        raise RetrievalUnavailable(self._fail_closed(
                            "metered ledger persistence failed (failing closed)"))

    # -- per-URL freshness ----------------------------------------------
    def cache_fresh(self, url: str, now: datetime | None = None) -> tuple[bool, str]:
        if self._cache_ttl_minutes <= 0:
            return False, ""
        current = now or _utcnow()
        with self._lock:
            if self._failed_reason:
                return False, self._failed_reason
            with _transaction_lock(self._store) as locked:
                if not locked:
                    # Cannot trust the freshness marker: deny, and remain failed
                    # closed so the subsequent reserve cannot spend budget either.
                    self._fail_closed("metered ledger lock unavailable (failing closed)")
                    return False, self._failed_reason
                self._state = self._load()
                if self._corrupt:
                    return False, "metered ledger corrupt (failing closed)"
                self._roll_day(_utc_day(current))
                stamped = _parse_dt((self._state.get("scraped") or {}).get(_url_key(url)))
                if stamped is not None:
                    if current - stamped < timedelta(minutes=self._cache_ttl_minutes):
                        return True, f"fresh scrape cache ({self._cache_ttl_minutes}m ttl)"
        return False, ""

    def mark_scraped(self, url: str, now: datetime | None = None) -> None:
        if self._cache_ttl_minutes <= 0:
            return
        current = now or _utcnow()
        with self._lock:
            if self._failed_reason:
                raise RetrievalUnavailable(self._failed_reason)
            with _transaction_lock(self._store) as locked:
                if not locked:
                    raise RetrievalUnavailable(self._fail_closed(
                        "metered ledger lock unavailable (failing closed)"))
                self._state = self._load()
                if self._corrupt:
                    raise RetrievalUnavailable("metered ledger corrupt (failing closed)")
                self._roll_day(_utc_day(current))
                markers = dict(self._state.get("scraped") or {})
                markers[_url_key(url)] = current.isoformat()
                # Bound the marker map; freshness is a short window, not history.
                if len(markers) > 256:
                    for key in sorted(markers, key=lambda k: str(markers.get(k)))[: len(markers) - 256]:
                        markers.pop(key, None)
                self._state["scraped"] = markers
                try:
                    self._save()
                except Exception:  # noqa: BLE001 - cannot persist => fail closed
                    raise RetrievalUnavailable(self._fail_closed(
                        "metered ledger persistence failed (failing closed)"))

    def describe(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or _utcnow()
        with self._lock:
            if not self._failed_reason:
                with _transaction_lock(self._store) as locked:
                    if not locked:
                        self._fail_closed("metered ledger lock unavailable (failing closed)")
                    else:
                        self._state = self._load()
                        self._roll_day(_utc_day(current))
        return {
            "day": self._state.get("day"),
            "day_used": int(self._state.get("day_used", 0)),
            "per_day": self._per_day,
            "per_cycle": self._per_cycle,
            "cycle_used": self._cycle_used,
            "cooldown_until": str(self._state.get("cooldown_until") or ""),
            "corrupt": self._corrupt,
            "available": not self._failed_reason and not self._corrupt,
            "unavailable_reason": self.unavailable_reason(),
        }


# --------------------------------------------------------------------------- #
# Firecrawl MCP adapter (native ctx.call_mcp only)
# --------------------------------------------------------------------------- #


def _join_mcp_text(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            if str(item.get("type") or "").lower() in {"text", "input_text", "output_text"} or "text" in item:
                value = item.get("text")
                if isinstance(value, str):
                    parts.append(value)
                elif value is not None:
                    parts.append(str(value))
        if sum(len(p) for p in parts) > 4_000_000:  # hard panic bound
            break
    return "\n".join(parts)


def _extract_mcp_text(result: Any, *, depth: int = 0) -> tuple[str, int | None]:
    """Best-effort, bounded text extraction from a native MCP result.

    Accepts an MCP content list, a Firecrawl-style object, or a raw string; if the
    extracted text is itself a JSON string it is decoded once and re-extracted.
    Returns ``(text, http_status_or_None)``. Never raises.
    """
    if depth > 3:
        return "", None
    status: int | None = None
    text = ""
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        for key in ("statusCode", "status_code", "status"):
            value = result.get(key)
            if isinstance(value, int):
                status = value
            elif isinstance(value, str) and value.strip().isdigit():
                status = int(value.strip())
        if result.get("ok") is False:
            return "", status
        if result.get("success") is False and not any(
                isinstance(result.get(k), (str, list, dict))
                for k in ("markdown", "text", "content", "data")):
            return "", status
        for key in ("markdown", "text", "content", "data", "result"):
            value = result.get(key)
            if isinstance(value, str) and value:
                text = value
                break
            if isinstance(value, list):
                joined = _join_mcp_text(value)
                if joined:
                    text = joined
                    break
            if isinstance(value, dict) and key in {"data", "result"}:
                nested, nested_status = _extract_mcp_text(value, depth=depth + 1)
                if nested:
                    return nested, status if status is not None else nested_status
    elif isinstance(result, list):
        text = _join_mcp_text(result)
    elif result is not None:
        text = str(result)

    stripped = text.strip()
    if stripped[:1] in {"{", "["}:
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            nested, nested_status = _extract_mcp_text(parsed, depth=depth + 1)
            if nested:
                return nested, status if status is not None else nested_status
    return text, status


def _unwrap_mcp_response(envelope: Any) -> tuple[str, str, int | None]:
    """``(text, error, http_status)`` from a ``ctx.call_mcp`` envelope.

    The host contract returns ``{"ok": bool, "result": ...}`` (or ``error``);
    a legacy injected client may return the bare result. Never raises.
    """
    if envelope is None:
        return "", "empty native MCP response", None
    if isinstance(envelope, dict) and ("ok" in envelope or "result" in envelope):
        if envelope.get("ok") is False:
            error = envelope.get("error")
            return "", (str(error) if error else "native MCP call failed"), None
        result = envelope.get("result")
        truncated = bool(envelope.get("truncated"))
        text, status = _extract_mcp_text(result)
        if truncated:
            text = text[: pf.DEFAULT_MAX_BYTES]
        return text, "", status
    text, status = _extract_mcp_text(envelope)
    return text, "", status


def _looks_like_429(text: str, status: int | None) -> bool:
    if status == 429:
        return True
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _429_MARKERS)


def _retry_after_seconds(text: str) -> int | None:
    match = _RETRY_AFTER_RE.search(str(text or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


class FirecrawlMCPAdapter:
    """Narrow public-page extraction through the supported ``ctx.call_mcp``.

    Sends ONLY ``{<url_arg>: <public https url>, <formats_arg>: ["markdown"]}``.
    No premium options (no crawl/browser/agent/json-extract/enhancements), no
    cookies/auth/private context, and no arbitrary tool invocation: the server and
    tool are the one configured pair, and every attempt is a reserved, persisted,
    capped local attempt.
    """

    def __init__(
        self,
        policy: dict[str, Any] | None,
        *,
        ctx: Any = None,
        store: Any | None = None,
        client: Any = None,
        now: datetime | None = None,
        ledger: MeteredAttemptLedger | None = None,
    ):
        self.policy = policy or {}
        self.ctx = ctx
        self.client = client
        self.config = resolve_firecrawl_config(self.policy)
        self.ledger = ledger or MeteredAttemptLedger(
            store,
            now=now,
            per_cycle=self.config["max_attempts_per_cycle"],
            per_day=self.config["max_attempts_per_day"],
            concurrency=self.config["concurrency"],
            cooldown_minutes=self.config["cooldown_minutes"],
            cache_ttl_minutes=self.config["cache_ttl_minutes"],
            fingerprint=self._fingerprint(),
        )

    def _fingerprint(self) -> str:
        cfg = self.config
        material = "|".join(str(cfg.get(key, "")) for key in (
            "server", "tool", "url_arg", "formats_arg", "max_age",
            "max_attempts_per_cycle", "max_attempts_per_day", "concurrency",
            "timeout_seconds", "cooldown_minutes", "cache_ttl_minutes"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    @property
    def enabled(self) -> bool:
        available, _ = self.available()
        return available

    def available(self) -> tuple[bool, str]:
        return firecrawl_route(self.policy, ctx=self.ctx, client=self.client)

    def describe(self, now: datetime | None = None) -> dict[str, Any]:
        available, reason = self.available()
        return {
            "available": available,
            "reason": reason,
            "server": self.config["server"],
            "tool": self.config["tool"],
            "schema_verified": self.config["schema_verified"],
            "budget": self.ledger.describe(now),
        }

    def scrape(
        self,
        url: str,
        *,
        timeout: float | None = None,
        max_bytes: int | None = None,
        now: datetime | None = None,
    ) -> bytes:
        """Scrape ONE public URL to bounded markdown bytes, or fail closed."""
        current = now or _utcnow()
        available, reason = self.available()
        if not available:
            raise RetrievalUnavailable(reason)
        # Private/unsafe URLs are rejected BEFORE any budget reservation or call.
        try:
            vetted = pf.validate_public_url(url, generic_opt_in=True).geturl()
        except pf.URLRejected as exc:
            raise RetrievalUnavailable(f"firecrawl url rejected: {exc}") from exc
        fresh, fresh_reason = self.ledger.cache_fresh(vetted, current)
        if fresh:
            raise RetrievalUnavailable(fresh_reason)  # avoid a repeat scrape
        reserved, reserve_reason = self.ledger.reserve(current)
        if not reserved:
            raise RetrievalUnavailable(reserve_reason)
        try:
            envelope = self._invoke(vetted, timeout=timeout)
        except RetrievalUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed call is "unavailable"
            raise RetrievalUnavailable(f"native MCP call failed ({type(exc).__name__})") from exc
        finally:
            self.ledger.release()

        text, error, status = _unwrap_mcp_response(envelope)
        combined = f"{error}\n{text}"
        if _looks_like_429(combined, status):
            until = self.ledger.record_429(current, retry_after_seconds=_retry_after_seconds(combined))
            raise RetrievalUnavailable(f"firecrawl rate limited (429); cooldown until {until}")
        if error:
            raise RetrievalUnavailable(f"firecrawl native error: {error[:200]}")
        if not text.strip():
            raise RetrievalUnavailable("firecrawl returned no content")
        body = text.encode("utf-8", "replace")
        classification = pf.classify_web_page(body)
        if classification:
            # A challenge/interstitial/error skeleton is NEVER evidence.
            raise RetrievalUnavailable(f"firecrawl {classification}: content unavailable")
        limit = int(max_bytes if max_bytes is not None else self.policy.get(
            "retrieval_fallback_max_bytes", pf.DEFAULT_MAX_BYTES))
        limit = max(1, limit)
        if len(body) > limit:
            body = body[:limit]
        self.ledger.record_success()
        self.ledger.mark_scraped(vetted, current)
        return body

    def _invoke(self, url: str, *, timeout: float | None) -> Any:
        cfg = self.config
        arguments = {cfg["url_arg"]: url, cfg["formats_arg"]: list(_FIRECRAWL_FORMATS)}
        if cfg.get("max_age") is not None:
            # Only ever sent when the owner supplied a verified schema/freshness bound.
            arguments["maxAge"] = int(cfg["max_age"])
        seconds = float(timeout if timeout is not None else cfg["timeout_seconds"])
        if _has_call_mcp(self.ctx):
            return self.ctx.call_mcp(cfg["server"], cfg["tool"], arguments, seconds)
        client = self.client
        caller = getattr(client, "call_mcp", None)
        if callable(caller):
            return caller(cfg["server"], cfg["tool"], arguments, seconds)
        if callable(client):
            return client(cfg["server"], cfg["tool"], arguments, seconds)
        raise RetrievalUnavailable("no native ctx.call_mcp available")


def build_firecrawl_adapter(
    policy: dict[str, Any] | None,
    *,
    ctx: Any = None,
    store: Any | None = None,
    client: Any = None,
    now: datetime | None = None,
) -> FirecrawlMCPAdapter | None:
    """Build the production adapter ONLY when the owner enabled+consented.

    Returns ``None`` for the neutral default (both flags off), so the ordinary
    collector byte path is completely unchanged unless the owner opts in.
    """
    cfg = resolve_firecrawl_config(policy)
    if not cfg["enabled"] or not cfg["consent"]:
        return None
    available, _ = firecrawl_route(policy, ctx=ctx, client=client)
    if not available and client is None:
        return None
    return FirecrawlMCPAdapter(policy, ctx=ctx, store=store, client=client, now=now)


__all__ = [
    "RenderUnavailable", "RetrievalUnavailable", "MeteredAttemptLedger",
    "FirecrawlMCPAdapter", "build_firecrawl_adapter", "resolve_firecrawl_config",
    "firecrawl_route", "native_render_route", "capabilities", "render_js_shell",
    "resolve_native_secret",
]
