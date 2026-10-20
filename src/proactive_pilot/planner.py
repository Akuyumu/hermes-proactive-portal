"""Model-assisted public query planner for adaptive watches.

The planner is deliberately OUTSIDE the zero-tool alert evaluator: it has its
own module, its own prompt and its own strict validation. It reuses the same
native Hermes invocation shape as the evaluator (``hermes chat -Q --query-file -
-t context_engine``), and it must prove the mounted toolset is empty before any
model call. A runner may be injected so tests never touch the network.

Rules:
- The planner sees ONLY the watch's public-safe fields (topic/scope/include/
  exclude). It never sees raw mail, calendar, chats or fetched page text.
- Output is strict JSON: a bounded list of public-safe queries. At most
  ``adaptive_max_queries_per_watch`` queries are accepted; more fail closed.
- Queries are NEVER required to be in the closed ``VETTED_INTERESTS`` list, so a
  novel but non-sensitive topic (e.g. "mycorrhizal networks") works.
- Any query containing code, commands, hosts/URLs, credentials, shell
  metacharacters or private-like identifiers is rejected.
- Plans are cached on the watch ``version`` with a bounded review cadence, not
  re-inferred every run. If the model is unavailable/malformed, the caller may
  fall back to an HONESTLY-LABELLED deterministic non-semantic plan; never a
  fabricated "successful model" result.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

from .evaluator import _extract_json, resolve_toolset_tools
from .publicfeeds import _EMAIL_RE, _SENSITIVE_MARKERS, _URL_RE

_HOST_RE = re.compile(r"\b[a-z0-9][a-z0-9-]*\.[a-z]{2,}\b", re.IGNORECASE)

PLAN_SCHEMA = 1
DEFAULT_MAX_QUERIES = 2
DEFAULT_QUERY_MAX_CHARS = 120
# Only scopes with a real existing approved public route are supported. "news"
# has no honest dedicated adaptive route (it must not silently become
# GitHub+Reddit), and general web/scholarship scopes have no verified route, so
# they remain unsupported even if the model proposes them.
_ACCEPTABLE_SCOPES = {"reddit", "github", "public"}

# Code/command/credential markers that must never appear in a public query.
_UNSAFE_CODE = (
    "sudo", "chmod", "chown", "curl", "wget", "ssh ", "scp ", "rm -rf",
    "subprocess", "exec(", "eval(", "os.system", "api_key", "apikey",
    "private key", "authorized_keys", "/etc/", "~/.ssh", "drop table",
    "powershell", "/bin/", "#!/",
)
_SHELL_META_RE = re.compile(r"[`;&|$(){}<>\\\n\r]")
_LONGNUM_RE = re.compile(r"\d{5,}")

PLANNER_SYSTEM = """\
You plan BOUNDED public search queries for ONE goal. You are not an executor and
you have no tools.

You receive only public-safe goal fields plus that goal's `origin`. Produce 1-2
short search queries that would find public evidence for the topic. Return strict
JSON only:
{"schema": 1, "queries": ["..."], "scope": "public",
 "semantic": true, "provenance": "model"}

Hard rules:
- Queries are short generic phrases (<= 120 chars). No URLs, no @emails, no
  hostnames, no shell commands, no code, no credentials, no long ID numbers.
- Honesty about criteria authority: when `origin` is "explicit" the watch
  include/exclude terms ARE user-stated and authoritative -- you MUST reflect
  the `include` terms and exclude the `exclude` terms (e.g. via a `-term`
  filter). When `origin` is "inferred" the criteria are model-inferred ADVISORY
  guidance only, NOT a user request or user-stated requirement: reflect them
  where they help recall, but never claim or imply the user asked for them.
- The watch `scope` is authoritative: never change it. If it is not a supported
  route, return an empty query list.
- Never include instructions, tool names, file paths or permissions.
- Do not invent private context. If the topic is unsuitable for public search,
  return {"schema": 1, "queries": [], "scope": "unsupported",
          "semantic": true, "provenance": "model"}.
"""


class PlannerError(RuntimeError):
    """Planning failed or produced invalid output; caller fails closed/falls back."""


class PlannerInvalid(PlannerError):
    """The planner RETURNED output that failed strict validation (adversarial or
    malformed). Unlike an unavailable planner, this is NOT retried with a
    deterministic query: it is rejected so bad model output cannot drive search.
    """


class Planner(Protocol):
    def plan(self, watch: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]: ...


def _collapsed(value: Any) -> str:
    return " ".join(str(value or "").split())


def validate_query(query: Any, *, max_chars: int = DEFAULT_QUERY_MAX_CHARS) -> str:
    if not isinstance(query, str):
        # Never coerce a number/bool/object into a query string: only an actual
        # string from the native planner is accepted.
        raise PlannerError("query must be a string")
    text = _collapsed(query)
    if not text:
        raise PlannerError("empty query")
    if len(text) > int(max_chars):
        raise PlannerError("query too long")
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        raise PlannerError("query contains sensitive-looking data")
    if any(marker in lowered for marker in _UNSAFE_CODE):
        raise PlannerError("query contains a code/command marker")
    if _EMAIL_RE.search(text) or _URL_RE.search(text) or _HOST_RE.search(text):
        raise PlannerError("query contains an email/url/host")
    if _LONGNUM_RE.search(text):
        raise PlannerError("query contains a long numeric identifier")
    if _SHELL_META_RE.search(text):
        raise PlannerError("query contains a shell metacharacter")
    if text.startswith("/") or ".." in text:
        raise PlannerError("query looks like a path")
    return text


def validate_plan(
    obj: Any,
    policy: dict[str, Any],
    *,
    watch: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Strict plan validation. Raises ``PlannerError`` on any violation.

    The ``schema`` tag is required and exact; query entries must be real strings
    (a number/bool/object is never coerced); ``semantic``/``provenance`` are
    type-checked when present. The EFFECTIVE scope always comes from the watch's
    requested authority scope, never from the model: a github watch can never be
    turned into reddit/public, and an unsupported watch scope stays unsupported
    even if the model claims ``public``.
    """
    if not isinstance(obj, dict):
        raise PlannerError("plan is not a JSON object")
    if obj.get("schema") != PLAN_SCHEMA:
        raise PlannerError("plan schema missing or unsupported")
    max_queries = max(1, int(policy.get("adaptive_max_queries_per_watch", DEFAULT_MAX_QUERIES)))
    max_chars = max(8, int(policy.get("adaptive_query_max_chars", DEFAULT_QUERY_MAX_CHARS)))
    raw_queries = obj.get("queries")
    if not isinstance(raw_queries, list):
        raise PlannerError("plan queries must be a list")
    if len(raw_queries) > max_queries:
        raise PlannerError(f"plan exceeds {max_queries} queries")
    queries = [validate_query(q, max_chars=max_chars) for q in raw_queries]
    if "semantic" in obj and not isinstance(obj.get("semantic"), bool):
        raise PlannerError("plan semantic must be a boolean")
    if "provenance" in obj and not isinstance(obj.get("provenance"), str):
        raise PlannerError("plan provenance must be a string")
    proposed_scope = _collapsed(obj.get("scope")) or "public"
    if len(proposed_scope) > 40:
        raise PlannerError("scope too long")
    watch_scope = _collapsed((watch or {}).get("scope"))
    scope = watch_scope or proposed_scope
    if scope in {"public", "github", "reddit"} and not queries:
        raise PlannerError("supported scope has no planned query; not successful coverage")
    plan = {
        "schema": PLAN_SCHEMA,
        "watch_id": str((watch or {}).get("id", "")),
        "watch_version": int((watch or {}).get("version", 0) or 0),
        "queries": queries,
        "scope": scope,
        "proposed_scope": proposed_scope,
        "semantic": bool(obj.get("semantic", True)),
        "provenance": _collapsed(obj.get("provenance")) or "model",
        # Truthful origin of the criteria the query was planned from. The model
        # can NEVER change this: it comes from the watch/goal, not the output.
        "origin": _collapsed((watch or {}).get("origin")) or "explicit",
        "advisory": _collapsed((watch or {}).get("origin")) == "inferred",
        "planned_at": (now.isoformat() if now is not None else ""),
    }
    return plan


def deterministic_plan(
    watch: dict[str, Any], policy: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    """Honest, deterministic non-semantic fallback (never called 'model').

    A criteria-only revision must still change the next acquisition: include
    terms are folded into the query and exclusions become ``-term`` filters, so
    the deterministic fallback is NOT blind to ``include``/``exclude``. If the
    composed query cannot be expressed safely/honestly within the bounded query
    size, the plan carries an explicit ``unsupported_reason`` and no query rather
    than silently ignoring the user's criteria.
    """
    max_chars = max(8, int(policy.get("adaptive_query_max_chars", DEFAULT_QUERY_MAX_CHARS)))
    topic = _collapsed(watch.get("public_topic"))
    include = [_collapsed(item) for item in (watch.get("include") or []) if _collapsed(item)]
    exclude = [_collapsed(item) for item in (watch.get("exclude") or []) if _collapsed(item)]
    composed = " ".join([topic] + include).strip()
    for term in exclude:
        composed = f"{composed} -{term}".strip()
    reason = ""
    query = ""
    if composed:
        try:
            query = validate_query(composed, max_chars=max_chars)
        except PlannerError as exc:
            reason = f"criteria cannot be expressed safely: {exc}"
    else:
        reason = "no public topic query could be derived"
    return {
        "schema": PLAN_SCHEMA,
        "watch_id": str(watch.get("id", "")),
        "watch_version": int(watch.get("version", 0) or 0),
        "queries": [query] if query else [],
        "scope": _collapsed(watch.get("scope")) or "public",
        "proposed_scope": _collapsed(watch.get("scope")) or "public",
        "semantic": False,
        "provenance": "deterministic-nonsemantic",
        "origin": _collapsed(watch.get("origin")) or "explicit",
        "advisory": _collapsed(watch.get("origin")) == "inferred",
        "unsupported_reason": reason,
        "planned_at": now.isoformat(),
    }


class FakePlanner:
    """Deterministic planner for tests; never touches the network."""

    def __init__(self, plan: Any):
        self._plan = plan
        self.calls: list[str] = []

    def plan(self, watch: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(str(watch.get("id", "")))
        result = self._plan(watch, policy) if callable(self._plan) else self._plan
        return json.loads(json.dumps(result))


class NativePlanner:
    """Native Hermes zero-tool planner with an injectable runner."""

    def __init__(self, hermes_bin: str = "hermes", profile: str = "default",
                 toolset: str = "context_engine",
                 runner: Callable[[list[str], str, int], tuple[int, str, str]] | None = None,
                 resolver: Callable[..., tuple[bool, list[str], str]] | None = None,
                 ctx: Any = None):
        self.hermes_bin = hermes_bin
        self.profile = profile
        self.toolset = toolset
        self.runner = runner
        self.resolver = resolver or resolve_toolset_tools
        # Optional host-built context: when present and usable, planning goes
        # through the bounded host-owned ``ctx.llm`` lane instead of a raw
        # subprocess. The subprocess route is retained ONLY as the explicit
        # compatibility fallback for a runtime whose context cannot be reached.
        self.ctx = ctx
        self.route = "subprocess"
        self.calls = 0

    def build_prompt(self, watch: dict[str, Any]) -> str:
        origin = _collapsed(watch.get("origin")) or "explicit"
        public = {
            "id": watch.get("id"),
            "public_topic": watch.get("public_topic"),
            "include": list(watch.get("include") or []),
            "exclude": list(watch.get("exclude") or []),
            "scope": watch.get("scope"),
            "origin": origin,
            "criteria_authority": (
                "model-inferred advisory guidance; NOT a user request and NOT "
                "user-stated" if origin == "inferred"
                else "explicit user-stated criteria; authoritative"
            ),
        }
        if origin == "inferred":
            guidance = (
                "This is an INFERRED, ADVISORY goal. Its include/exclude criteria "
                "were inferred by a model, NOT stated by the user. Reflect them as "
                "advisory recall hints only and never present them as a user request."
            )
        else:
            guidance = (
                "This is an EXPLICIT user watch. Its include/exclude criteria ARE "
                "user-stated and authoritative; reflect include terms and exclude "
                "exclude terms."
            )
        return (PLANNER_SYSTEM + "\n\nWATCH:\n"
                + json.dumps(public, ensure_ascii=False)
                + "\n\nORIGIN GUIDANCE: " + guidance
                + "\n\nReturn only the JSON object.")

    def _native_lane(self, policy: dict[str, Any]) -> Any:
        if self.ctx is None:
            return None
        from .native_runtime import build_model_lane

        lane = build_model_lane(self.ctx, policy, path="planner",
                                purpose="secretary.plan")
        if lane.available:
            return lane
        raise PlannerError(f"configured model lane unavailable: {lane.route.reason}")

    def plan(self, watch: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
        cfg = policy.get("evaluator", {}) or {}
        prompt = self.build_prompt(watch)
        lane = self._native_lane(policy)
        if lane is not None:
            self.calls += 1
            try:
                obj = lane.complete_json(
                    instructions=prompt,
                    schema_name="secretary.plan",
                    max_tokens=int(policy.get("adaptive_max_tokens", 1200)),
                    timeout=float(policy.get("adaptive_plan_timeout_seconds", 60)),
                    temperature=0.0,
                )
            except Exception as exc:  # noqa: BLE001 - sanitized route failure
                raise PlannerError(
                    f"planner ctx.llm failed ({type(exc).__name__})") from exc
            self.route = "ctx.llm"
            try:
                plan = validate_plan(obj, policy, watch=watch)
            except PlannerError as exc:
                raise PlannerInvalid(f"planner output failed validation: {exc}") from exc
            plan["semantic"] = True
            plan["provenance"] = "model"
            return plan
        resolved_ok, names, detail = self.resolver(
            self.toolset, hermes_bin=self.hermes_bin,
            timeout=int(cfg.get("resolve_timeout_seconds", 30)),
        )
        if not resolved_ok:
            raise PlannerError(f"toolset resolution failed: {detail}")
        if names:
            raise PlannerError(f"planner toolset exposes {len(names)} tools; refusing")
        argv = [
            self.hermes_bin, "-p", self.profile, "chat", "-Q",
            "--query-file", "-", "--source", "tool",
            "--run-budget", str(int(cfg.get("run_budget_seconds", 90))),
            "--max-turns", str(int(policy.get("adaptive_max_turns", 4))),
            "-t", self.toolset,
        ]
        timeout = int(policy.get("adaptive_plan_timeout_seconds", 60))
        self.calls += 1
        if self.runner is not None:
            rc, out, err = self.runner(argv, prompt, timeout)
        else:
            try:
                proc = subprocess.run(argv, shell=False, input=prompt, text=True,
                                      capture_output=True, timeout=timeout, check=False)
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                raise PlannerError(f"planner invocation failed: {exc}") from exc
            rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
        if rc != 0:
            raise PlannerError(f"planner exit {rc}")
        obj = _extract_json(out)
        if obj is None:
            raise PlannerError("planner returned no parseable JSON")
        try:
            plan = validate_plan(obj, policy, watch=watch)
        except PlannerError as exc:
            raise PlannerInvalid(f"planner output failed validation: {exc}") from exc
        # The native invocation OWNS provenance/semantic: generated labels are
        # never trusted as evidence of a real semantic model plan.
        self.route = "subprocess"
        plan["semantic"] = True
        plan["provenance"] = "model"
        return plan


def plan_for_watch(
    watch: dict[str, Any],
    policy: dict[str, Any],
    planner: Any,
    *,
    now: datetime,
    force: bool = False,
    plan_timeout: float | None = None,
) -> tuple[dict[str, Any], bool]:
    """Return ``(plan, used_model)`` honouring the per-version plan cache.

    A cached plan is reused while the watch version is unchanged AND the review
    cadence has not elapsed, so inference does not run every scheduled cycle.
    The watch dict is mutated in place with the cached plan for persistence.

    ``plan_timeout`` overrides the policy planner bound so the caller can clamp
    the model phase to the remaining overall collector budget; it is a SEPARATE
    non-HTTP phase and never charged against the public HTTP budget.
    """
    version = int(watch.get("version", 0) or 0)
    cached = watch.get("plan")
    cadence = max(1, int(policy.get("adaptive_plan_cadence_minutes", 360)))
    if not force and isinstance(cached, dict) and int(cached.get("watch_version", -1)) == version:
        stamp = str(cached.get("planned_at") or "")
        try:
            fresh = now - datetime.fromisoformat(stamp) < timedelta(minutes=cadence)
        except ValueError:
            fresh = False
        if fresh:
            return json.loads(json.dumps(cached)), False
    plan: dict[str, Any] | None = None
    used_model = False
    rejected_invalid = False
    if planner is not None:
        # A separate, bounded planner timeout: the planner is a non-HTTP phase
        # and must never be charged against (or silently consume) the public HTTP
        # fetch budget. A hung/injected planner is bounded here and falls back
        # honestly instead of stalling the cycle.
        from .watchdog import deadline_guard

        candidate: Any = None
        try:
            base = policy.get("adaptive_plan_timeout_seconds", 60) if plan_timeout is None else plan_timeout
            timeout = max(0.05, float(base))
            with deadline_guard(timeout, lambda: TimeoutError("planner exceeded bounded timeout")):
                candidate = planner.plan(watch, policy)
        except PlannerInvalid:
            # Adversarial/malformed model output: REJECT, never retry with a
            # fabricated deterministic query.
            rejected_invalid = True
        except Exception:  # noqa: BLE001 - unavailable/timeout -> honest fallback
            candidate = None
        if candidate is not None and not rejected_invalid:
            try:
                plan = validate_plan(candidate, policy, watch=watch, now=now)
                used_model = True
            except PlannerError:
                plan = None
    if plan is None:
        if rejected_invalid:
            plan = _rejected_plan(watch, policy, now=now)
        else:
            plan = deterministic_plan(watch, policy, now=now)
    watch["plan"] = plan
    watch["plan_version"] = int(plan.get("watch_version", version))
    watch["plan_at"] = str(plan.get("planned_at") or now.isoformat())
    return plan, used_model


def _rejected_plan(watch: dict[str, Any], policy: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """An explicit, query-less rejection of invalid/adversarial planner output.

    Distinct from the ``deterministic-nonsemantic`` fallback used when the model
    is merely UNAVAILABLE: no query is produced, so bad model output can never
    drive a public search, and the provenance records the rejection.
    """
    return {
        "schema": PLAN_SCHEMA,
        "watch_id": str(watch.get("id", "")),
        "watch_version": int(watch.get("version", 0) or 0),
        "queries": [],
        "scope": _collapsed(watch.get("scope")) or "public",
        "proposed_scope": _collapsed(watch.get("scope")) or "public",
        "semantic": False,
        "provenance": "model-invalid-rejected",
        "origin": _collapsed(watch.get("origin")) or "explicit",
        "advisory": _collapsed(watch.get("origin")) == "inferred",
        "unsupported_reason": "planner output failed validation; rejected",
        "planned_at": now.isoformat(),
    }


def is_supported_scope(scope: Any) -> bool:
    return _collapsed(scope) in _ACCEPTABLE_SCOPES
