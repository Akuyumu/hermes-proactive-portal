"""Native Hermes web-search bridge (fixed-function, non-agent, cron-compatible).

The already-configured Hermes ``web_search_tool`` lives in the native runtime
(``tools/web_tools.py``) and returns a JSON **string** of the shape::

    {"success": true, "data": {"web": [
        {"title": "...", "url": "https://...", "description": "...", ...}
    ]}}

This module is a THIN, FINITE adapter. It is deliberately NOT an evaluator tool
and exposes no arbitrary command execution:

- The runtime command is resolved once via ``hermes --print-runtime-command``
  (native support). That command is a native isolated interpreter that
  dispatches ``hermes_cli.main`` -- it is **not** a generic
  ``--run-module/--function/--args`` executor. So a fixed public-search entry
  point replaces ONLY the known ``hermes_cli.main`` dispatch with a static
  ``from tools.web_tools import web_search_tool`` and reads one query from a
  separate ``sys.argv[1]`` JSON payload. The native path/env/``hermes_bootstrap``
  setup is preserved verbatim. Any other prefix shape, module or function is
  rejected fail-closed; no arbitrary module/function is ever launched.
- A ``runner`` callable is injectable, so tests never touch a subprocess and the
  parent can wire the real native runtime later. This stage performs NO live
  search and NO canary; the operational live canary is approval-blocked/pending.
- The provider's raw error text is never surfaced; failures are mapped to a
  fixed taxonomy (``failed``/``unsupported``/``rate_limited``/``invalid``) so no
  credential, token or provider body can leak into diagnostics.
- A healthy **empty** result (``success: true`` and ``web: []``) is reported as
  ``empty`` -- NEVER fabricated into a feed or a fake result URL.
- Bounded by ``max_queries_per_cycle``, a per-query TTL cache, and a hard
  per-run rate-limit stop (a 429 is never retried or bypassed in the cycle).
"""
from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence
from urllib.parse import urlsplit

from .publicfeeds import URLRejected, canonical_host, validate_public_url

# -- status taxonomy ---------------------------------------------------------

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"
STATUS_UNSUPPORTED = "unsupported"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_INVALID = "invalid"

_FAILED_STATUSES = {STATUS_FAILED, STATUS_UNSUPPORTED, STATUS_RATE_LIMITED, STATUS_INVALID}

_RATE_MARKERS = ("429", "rate limit", "rate-limit", "too many requests", "quota")
_UNSUPPORTED_MARKERS = (
    "not found", "no module", "unknown module", "unrecognized", "unsupported",
    "no such", "command not found", "cannot find", "missing runtime",
)

RUNNER_TIMEOUT = 30
DEFAULT_MAX_QUERIES_PER_CYCLE = 2
DEFAULT_CACHE_TTL_MINUTES = 60
DEFAULT_RATE_LIMIT_BACKOFF_MINUTES = 60
# Fixed-function native contract. The ONLY module/function pair the bridge will
# ever launch; arbitrary policy overrides are rejected (never executed).
FIXED_MODULE = "tools.web_tools"
FIXED_FUNCTION = "web_search_tool"
DEFAULT_MODULE = FIXED_MODULE
DEFAULT_FUNCTION = FIXED_FUNCTION
# The exact dispatch substring observed in the real
# ``hermes --print-runtime-command`` bootstrap output. If it is absent or
# ambiguous the prefix is rejected instead of being extended with invented flags.
BOOTSTRAP_DISPATCH = "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
FIXED_SEARCH_CODE = (
    "import json; from tools.web_tools import web_search_tool; "
    "payload = json.loads(sys.argv[1]); "
    "print(web_search_tool(payload['query'], limit=5))"
)
MAX_DESCRIPTION_CHARS = 400
MAX_TITLE_CHARS = 200
SEARCH_CACHE_ARTIFACT = "adaptive_search_cache.json"
CACHE_SCHEMA = 1


class BridgeError(RuntimeError):
    """The bridge could not be invoked at the transport level (safe label only)."""


class Runner(Protocol):
    def run(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]: ...


@dataclass
class SearchHit:
    title: str
    url: str
    description: str = ""
    host: str = ""
    published: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "url": self.url, "description": self.description,
                "host": self.host, "published": self.published}


@dataclass
class SearchOutcome:
    query: str
    status: str
    hits: list[SearchHit] = field(default_factory=list)
    raw_count: int = 0
    error: str = ""
    from_cache: bool = False
    cache_at: str = ""
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def healthy_empty(self) -> bool:
        """A genuine provider ``success`` with zero (usable) results."""
        return self.status == STATUS_EMPTY

    @property
    def failed(self) -> bool:
        return self.status in _FAILED_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query, "status": self.status, "count": len(self.hits),
            "raw_count": self.raw_count, "error": self.error,
            "from_cache": self.from_cache, "latency_ms": self.latency_ms,
            "hosts": sorted({h.host for h in self.hits if h.host}),
        }


class SearchBudget:
    """One finite per-cycle query allowance shared by all callers."""

    def __init__(self, total: int):
        self.total = max(0, int(total))
        self.used = 0

    def remaining(self) -> int:
        return max(0, self.total - self.used)

    def take(self, want: int = 1) -> int:
        got = min(self.remaining(), max(0, int(want)))
        self.used += got
        return got


def _default_url_validator(url: str) -> Any:
    """Discovered hosts are unknown by construction: relax ONLY the static host
    allowlist. Scheme/credential/port/secret-query checks still apply, and the
    discovery layer adds all-resolved-IP public-DNS vetting before any fetch."""
    return validate_public_url(url, generic_opt_in=True)


def _safe_label(text: Any) -> str:
    """Map arbitrary provider/transport text to a fixed non-sensitive label."""
    lowered = str(text or "").lower()
    if any(marker in lowered for marker in _RATE_MARKERS):
        return STATUS_RATE_LIMITED
    if any(marker in lowered for marker in _UNSUPPORTED_MARKERS):
        return STATUS_UNSUPPORTED
    return STATUS_FAILED


def _parse_stdout_json(stdout: Any) -> Any:
    """Parse a JSON object, tolerating a double-encoded JSON **string**.

    The native tool returns a JSON string, which may arrive either as a real
    object (already decoded by a runner) or as a string containing JSON. A
    malformed body returns ``None`` (never a fabricated empty object).
    """
    if isinstance(stdout, (dict, list)):
        return stdout
    text = str(stdout or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def _hit_from_row(row: Any) -> SearchHit | None:
    if not isinstance(row, dict):
        return None
    url = str(row.get("url") or row.get("link") or "").strip()
    title = " ".join(str(row.get("title") or row.get("name") or "").split())[:MAX_TITLE_CHARS]
    description = " ".join(
        str(row.get("description") or row.get("snippet") or row.get("summary") or "").split()
    )[:MAX_DESCRIPTION_CHARS]
    published = str(row.get("published") or row.get("date") or "")[:60]
    if not url:
        return None
    return SearchHit(title=title, url=url, description=description,
                     host=canonical_host(urlsplit(url).hostname or ""), published=published)


def outcome_from_payload(
    query: str,
    payload: Any,
    latency: int = 0,
    *,
    url_validator: Callable[[str], Any] | None = None,
) -> SearchOutcome:
    """Map a decoded ``web_search_tool`` payload to a bounded outcome.

    Distinguishes a healthy empty result (``success`` + no usable rows) from a
    failure/unsupported/rate-limited/invalid payload. Non-public URLs are dropped
    and never fabricated into a hit.
    """
    validate = url_validator or _default_url_validator
    if not isinstance(payload, dict) or not isinstance(payload.get("success"), bool):
        return SearchOutcome(query=query, status=STATUS_INVALID,
                             error="malformed search payload", latency_ms=latency)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if payload.get("success") is False:
        label = _safe_label(payload.get("error") or data.get("error"))
        return SearchOutcome(query=query, status=label, error=label, latency_ms=latency)
    web = data.get("web")
    if not isinstance(web, list):
        return SearchOutcome(query=query, status=STATUS_INVALID,
                             error="malformed search payload", latency_ms=latency)
    hits: list[SearchHit] = []
    for row in web:
        hit = _hit_from_row(row)
        if hit is None:
            continue
        try:
            validate(hit.url)
        except URLRejected:
            continue
        hits.append(hit)
    if not hits:
        return SearchOutcome(query=query, status=STATUS_EMPTY, raw_count=len(web),
                             latency_ms=latency)
    return SearchOutcome(query=query, status=STATUS_OK, hits=hits,
                         raw_count=len(web), latency_ms=latency)


def _outcome_to_dict(outcome: SearchOutcome) -> dict[str, Any]:
    """Serialize an outcome for the owner-only persistent cache."""
    return {
        "query": outcome.query,
        "status": outcome.status,
        "error": outcome.error,
        "raw_count": outcome.raw_count,
        "latency_ms": outcome.latency_ms,
        "hits": [h.to_dict() for h in outcome.hits],
    }


def _outcome_from_dict(data: Any) -> SearchOutcome | None:
    if not isinstance(data, dict):
        return None
    status = str(data.get("status") or "")
    if status not in {STATUS_OK, STATUS_EMPTY, STATUS_FAILED, STATUS_UNSUPPORTED,
                      STATUS_RATE_LIMITED, STATUS_INVALID}:
        return None
    hits: list[SearchHit] = []
    for row in data.get("hits") or []:
        if not isinstance(row, dict):
            continue
        hits.append(SearchHit(
            title=str(row.get("title") or ""),
            url=str(row.get("url") or ""),
            description=str(row.get("description") or ""),
            host=str(row.get("host") or ""),
            published=str(row.get("published") or ""),
        ))
    return SearchOutcome(
        query=str(data.get("query") or ""), status=status, hits=hits,
        raw_count=int(data.get("raw_count") or 0), error=str(data.get("error") or ""),
        latency_ms=int(data.get("latency_ms") or 0),
    )


class NativeSubprocessRunner:
    """Fixed-function subprocess runner for the native runtime (no shell)."""

    def run(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        import subprocess

        try:
            proc = subprocess.run(list(argv), shell=False, capture_output=True,
                                  text=True, timeout=max(1, int(timeout)), check=False)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return 1, "", "invocation failed"
        return proc.returncode, proc.stdout or "", proc.stderr or ""


def purge_cached_watch(store: Any, watch_id: str) -> int:
    """Drop every persisted query outcome for a retired watch (stop/forget).

    Called by the watch lifecycle so a retired watch cannot replay a cached
    result or a latched backoff on a later cycle. Owner-only artifact; no-op
    when the store or watch id is missing.
    """
    wid = str(watch_id or "")
    if store is None or not wid:
        return 0
    try:
        data = store.load(SEARCH_CACHE_ARTIFACT,
                          {"schema": CACHE_SCHEMA, "entries": {}, "rate_limited_until": 0.0})
    except Exception:  # noqa: BLE001 - never block lifecycle cleanup
        return 0
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return 0
    prefix = f"{wid}|v"
    removed = [k for k in data["entries"] if k.startswith(prefix)]
    for key in removed:
        data["entries"].pop(key, None)
    if removed:
        data["rate_limited_until"] = 0.0
        try:
            store.save(SEARCH_CACHE_ARTIFACT, data)
        except Exception:  # noqa: BLE001
            pass
    return len(removed)


def build_native_bridge(    policy: dict[str, Any],
    *,
    store: Any = None,
    watch_id: str = "",
    watch_version: int = 0,
    revision_token: str = "",
    runner: Any = None,
    clock: Callable[[], float] | None = None,
    wall_clock: Callable[[], float] | None = None,
) -> NativeSearchBridge:
    """Construct the production bridge from policy (fixed function, no argv).

    The runtime is resolved via ``hermes --print-runtime-command``; the known
    ``hermes_cli.main`` dispatch is then replaced with a static import of the
    fixed ``tools.web_tools.web_search_tool`` and one JSON query. The module and
    function are NOT configurable: any policy value other than the fixed pair
    makes ``search`` fail closed with ``unsupported`` and nothing is launched.
    When ``store`` is supplied the per-query cache and 429 backoff persist
    owner-only, keyed to the watch revision. Tests inject ``runner``/``store``
    and never touch a subprocess.
    """
    cadence_minutes = int(policy.get("native_search_plan_cadence_minutes",
                                     policy.get("adaptive_plan_cadence_minutes", 360)) or 360)
    return NativeSearchBridge(
        runner if runner is not None else NativeSubprocessRunner(),
        hermes_bin=str(policy.get("native_search_hermes_bin") or "hermes"),
        module=str(policy.get("native_search_module") or DEFAULT_MODULE),
        function=str(policy.get("native_search_function") or DEFAULT_FUNCTION),
        policy=policy,
        clock=clock,
        cache_store=store,
        cache_watch_id=watch_id,
        cache_watch_version=watch_version,
        cache_revision_token=revision_token,
        cache_cadence_seconds=cadence_minutes * 60,
        wall_clock=wall_clock,
    )


class NativeSearchBridge:
    """Fixed-function adapter over the native ``web_search_tool``.

    Invocation contract (validated by the parent's native canary later):
      prefix = ``hermes --print-runtime-command`` output: a native isolated
               interpreter whose bootstrap ends with
               ``runpy.run_module('hermes_cli.main', ...)``
      argv   = prefix with that dispatch replaced by a STATIC
               ``from tools.web_tools import web_search_tool`` call, plus one
               trailing ``sys.argv`` JSON payload ``{"query": "<q>"}``.
    """

    def __init__(
        self,
        runner: Runner,
        *,
        hermes_bin: str = "hermes",
        module: str = DEFAULT_MODULE,
        function: str = DEFAULT_FUNCTION,
        policy: dict[str, Any] | None = None,
        url_validator: Callable[[str], Any] | None = None,
        clock: Callable[[], float] | None = None,
        cache_store: Any = None,
        cache_watch_id: str = "",
        cache_watch_version: int = 0,
        cache_cadence_seconds: int | None = None,
        wall_clock: Callable[[], float] | None = None,
        cache_revision_token: str = "",
    ):
        self.runner = runner
        self.hermes_bin = hermes_bin
        self.module = module
        self.function = function
        self.policy = policy or {}
        self.url_validator = url_validator or _default_url_validator
        self._now = clock or time.monotonic
        self.max_queries = int(self.policy.get("native_search_max_queries_per_cycle",
                                              DEFAULT_MAX_QUERIES_PER_CYCLE))
        self.cache_ttl_seconds = int(self.policy.get("native_search_cache_ttl_minutes",
                                                     DEFAULT_CACHE_TTL_MINUTES)) * 60
        self.backoff_seconds = int(self.policy.get("native_search_rate_limit_backoff_minutes",
                                                   DEFAULT_RATE_LIMIT_BACKOFF_MINUTES)) * 60
        # The module/function pair is fixed. Nothing arbitrary may be launched.
        self._config_ok = (module == FIXED_MODULE and function == FIXED_FUNCTION)
        self._prefix: list[str] | None = None
        self._prefix_error = ""
        self._cache: dict[str, tuple[float, SearchOutcome]] = {}
        self._rate_limited_until = 0.0
        self.budget = SearchBudget(self.max_queries)
        self.rate_limit_hits = 0
        # Persistent, owner-only cross-process cache. When absent the bridge
        # keeps its in-memory TTL cache only (tests). When present, query
        # outcomes AND 429 backoff survive new bridge/process objects, keyed by
        # watch revision, so an UNCHANGED watch makes zero extra backend calls
        # until the review cadence elapses. A watch revision invalidates the
        # cached entries for the previous version.
        self._store = cache_store
        self._watch_id = str(cache_watch_id or "")
        self._watch_version = int(cache_watch_version or 0)
        # Safety/context revision (e.g. the inferred privacy decision): cached
        # search results are bound to it, so a changed safety decision or context
        # revision can never replay a previously cached result.
        self._revision_token = str(cache_revision_token or "")
        cadence = int(cache_cadence_seconds or self.cache_ttl_seconds)
        self._cache_ttl_seconds = max(self.cache_ttl_seconds, cadence)
        self._wall = wall_clock or time.time

    def bind_watch(self, watch_id: str, watch_version: int,
                   revision_token: str = "") -> None:
        """Bind query cache identity (watch + safety/context revision)."""
        token = str(revision_token or "")
        if ((self._watch_id, self._watch_version, self._revision_token)
                != (str(watch_id), int(watch_version), token)):
            self._cache.clear()
        self._watch_id = str(watch_id)
        self._watch_version = int(watch_version)
        self._revision_token = token

    # -- persistent cache ---------------------------------------------------
    def _cache_key(self, query: str) -> str:
        # Backward compatible: with no safety/context revision the key is exactly
        # the legacy ``watch|version|query`` shape; a revision (inferred goals)
        # adds a binding segment so a changed decision/context cannot replay.
        if self._revision_token:
            return (f"{self._watch_id}|v{self._watch_version}|"
                    f"{self._revision_token}|{query}")
        return f"{self._watch_id}|v{self._watch_version}|{query}"

    def _cache_load(self) -> dict[str, Any]:
        if self._store is None:
            return {"schema": CACHE_SCHEMA, "entries": {}, "rate_limited_until": 0.0}
        data = self._store.load(SEARCH_CACHE_ARTIFACT,
                                {"schema": CACHE_SCHEMA, "entries": {},
                                 "rate_limited_until": 0.0})
        if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA:
            return {"schema": CACHE_SCHEMA, "entries": {}, "rate_limited_until": 0.0}
        if not isinstance(data.get("entries"), dict):
            data["entries"] = {}
        return data

    def _cache_save(self, data: dict[str, Any]) -> None:
        if self._store is None:
            return
        try:
            self._store.save(SEARCH_CACHE_ARTIFACT, data)
        except Exception:  # noqa: BLE001 - cache persistence never breaks search
            pass

    def _purge_other_versions(self, data: dict[str, Any]) -> None:
        """Retire cached outcomes for previous versions of THIS watch."""
        prefix = f"{self._watch_id}|v"
        keep_prefix = f"{self._watch_id}|v{self._watch_version}|"
        stale = [k for k in data.get("entries", {})
                 if k.startswith(prefix) and not k.startswith(keep_prefix)]
        for key in stale:
            data["entries"].pop(key, None)

    def purge_watch(self) -> None:
        """Remove every cached query outcome for this watch (stop/forget)."""
        if self._store is None:
            return
        data = self._cache_load()
        prefix = f"{self._watch_id}|v"
        for key in [k for k in data.get("entries", {}) if k.startswith(prefix)]:
            data["entries"].pop(key, None)
        data["rate_limited_until"] = 0.0
        self._cache_save(data)

    def _persistent_lookup(self, query: str, now: float) -> SearchOutcome | None:
        if self._store is None:
            return None
        data = self._cache_load()
        self._purge_other_versions(data)
        until = float(data.get("rate_limited_until") or 0.0)
        if now < until:
            self._cache_save(data)
            return SearchOutcome(query=query, status=STATUS_RATE_LIMITED,
                                 error="rate limited; backoff active")
        entry = data.get("entries", {}).get(self._cache_key(query))
        if isinstance(entry, dict):
            at = float(entry.get("at") or 0.0)
            if now - at < self._cache_ttl_seconds:
                outcome = _outcome_from_dict(entry.get("outcome"))
                if outcome is not None:
                    fresh = SearchOutcome(**{**outcome.__dict__, "from_cache": True})
                    self._cache_save(data)
                    return fresh
        self._cache_save(data)
        return None

    def _persistent_store(self, query: str, outcome: SearchOutcome, now: float) -> None:
        if self._store is None:
            return
        data = self._cache_load()
        self._purge_other_versions(data)
        data.setdefault("entries", {})[self._cache_key(query)] = {
            "at": now, "watch_id": self._watch_id, "watch_version": self._watch_version,
            "outcome": _outcome_to_dict(outcome),
        }
        if outcome.status == STATUS_RATE_LIMITED:
            data["rate_limited_until"] = now + self.backoff_seconds
        self._cache_save(data)

    # -- runtime resolution -------------------------------------------------
    def runtime_prefix(self) -> list[str]:
        if self._prefix is not None:
            return self._prefix
        try:
            rc, out, err = self.runner.run([self.hermes_bin, "--print-runtime-command"], RUNNER_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - never echo transport internals
            self._prefix_error = _safe_label(exc)
            raise BridgeError(self._prefix_error) from exc
        if rc != 0 or not str(out or "").strip():
            self._prefix_error = _safe_label(err or out)
            raise BridgeError(self._prefix_error)
        prefix = self._parse_prefix(out)
        if not prefix:
            self._prefix_error = STATUS_UNSUPPORTED
            raise BridgeError(self._prefix_error)
        self._prefix = prefix
        return prefix

    @staticmethod
    def _parse_prefix(out: str) -> list[str]:
        text = str(out or "").strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            value = None
        if isinstance(value, list):
            return [str(x) for x in value if str(x)]
        if isinstance(value, dict):
            command = value.get("command") or value.get("argv") or value.get("cmd")
            if isinstance(command, list):
                return [str(x) for x in command if str(x)]
            if isinstance(command, str):
                return shlex.split(command)
        # Fall back to the LAST non-empty line (a bootstrap may print a banner).
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line:
                return shlex.split(line)
        return []

    def build_argv(self, query: str, prefix: Sequence[str] | None = None) -> list[str]:
        # Only the fixed module/function pair is ever launched. An arbitrary
        # policy override fails closed instead of naming another module.
        if not self._config_ok:
            raise BridgeError(STATUS_UNSUPPORTED)
        base = list(prefix if prefix is not None else self.runtime_prefix())
        # The native runtime command already dispatches hermes_cli.main. It is
        # not a generic --function runner, so there are no --run-module/
        # --function/--args flags to append. Replace ONLY the exact known
        # dispatch with the static public-search entry point, preserving the
        # native path/env/hermes_bootstrap setup verbatim. Any other shape is
        # rejected rather than extended with invented flags.
        try:
            code_index = base.index("-c") + 1
            bootstrap = str(base[code_index])
        except (ValueError, IndexError):
            raise BridgeError(STATUS_UNSUPPORTED)
        if bootstrap.count(BOOTSTRAP_DISPATCH) != 1:
            raise BridgeError(STATUS_UNSUPPORTED)
        base[code_index] = bootstrap.replace(BOOTSTRAP_DISPATCH, FIXED_SEARCH_CODE)
        return base + [json.dumps({"query": query}, ensure_ascii=False)]

    # -- search -------------------------------------------------------------
    def search(self, query: str, *, now: float | None = None,
               timeout: float | None = None) -> SearchOutcome:
        query = " ".join(str(query or "").split())
        now = self._now() if now is None else float(now)
        # Cross-process persisted cache first: an unchanged watch revision makes
        # zero extra backend calls until its review cadence elapses.
        if self._store is not None:
            persisted = self._persistent_lookup(query, self._wall())
            if persisted is not None:
                return persisted
        cached = self._cache.get(query)
        if cached is not None and now - cached[0] < self.cache_ttl_seconds:
            outcome = cached[1]
            fresh = SearchOutcome(**{**outcome.__dict__, "from_cache": True})
            return fresh
        if now < self._rate_limited_until:
            return SearchOutcome(query=query, status=STATUS_RATE_LIMITED,
                                 error="rate limited; backoff active")
        if self.budget.remaining() <= 0:
            return SearchOutcome(query=query, status=STATUS_FAILED,
                                 error="per-cycle search budget exhausted")
        self.budget.take(1)
        started = self._now()
        if timeout is None:
            run_timeout = RUNNER_TIMEOUT
        else:
            run_timeout = int(max(1, min(RUNNER_TIMEOUT, float(timeout))))
        try:
            prefix = self.runtime_prefix()
            argv = self.build_argv(query, prefix)
        except BridgeError as exc:
            return SearchOutcome(query=query, status=STATUS_UNSUPPORTED, error=str(exc))
        try:
            rc, out, err = self.runner.run(argv, run_timeout)
        except Exception:  # noqa: BLE001 - fixed safe label
            return SearchOutcome(query=query, status=STATUS_FAILED, error="search invocation failed")
        latency = int((self._now() - started) * 1000)
        if rc != 0:
            status = _safe_label(err or out)
            if status == STATUS_RATE_LIMITED:
                self._rate_limited_until = now + self.backoff_seconds
                self.rate_limit_hits += 1
            outcome = SearchOutcome(query=query, status=status, error=status, latency_ms=latency)
            self._cache[query] = (now, outcome)
            self._persistent_store(query, outcome, self._wall())
            return outcome
        payload = _parse_stdout_json(out)
        outcome = self._outcome_from_payload(query, payload, latency)
        if outcome.status == STATUS_RATE_LIMITED:
            self._rate_limited_until = now + self.backoff_seconds
            self.rate_limit_hits += 1
        self._cache[query] = (now, outcome)
        self._persistent_store(query, outcome, self._wall())
        return outcome

    def _outcome_from_payload(self, query: str, payload: Any, latency: int) -> SearchOutcome:
        return outcome_from_payload(query, payload, latency, url_validator=self.url_validator)


class FakeSearchBridge:
    """Deterministic fake bridge for tests: never invokes a runtime or network.

    ``responses`` maps a query to either a full payload dict (``success``/``data``)
    or a pre-built :class:`SearchOutcome`. Unmapped queries raise so a test can
    prove no unplanned query was issued.
    """

    def __init__(self, responses: dict[str, Any] | None = None,
                 default: Any = None, *, fail: Exception | None = None):
        self.responses = responses or {}
        self.default = default
        self.fail = fail
        self.calls: list[str] = []

    def search(self, query: str, **kwargs: Any) -> SearchOutcome:
        self.calls.append(query)
        if self.fail is not None:
            raise self.fail
        spec = self.responses.get(query, self.default)
        if spec is None:
            raise AssertionError(f"no fake search response for query: {query!r}")
        if isinstance(spec, SearchOutcome):
            return spec
        if callable(spec):
            spec = spec(query)
        payload = spec
        if isinstance(payload, SearchOutcome):
            return payload
        return outcome_from_payload(query, payload, 0)
