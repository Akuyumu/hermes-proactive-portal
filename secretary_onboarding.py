"""Resumable, validated onboarding state for the proactive secretary.

The foreground Hermes agent owns the conversation. This module only keeps typed,
profile-scoped state, validates proposed values, and returns exact readback. It
never reads sources, arms jobs, stores credentials, or treats model assertions as
user consent.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .secretary_models import (ROUTE_SETTING_KEYS, effective_route_status,
                               validate_route_settings)

STATE_KEY = "onboarding_state"
SCHEMA = 1
DEFAULT_ALLOWANCE = 5
STEP_ORDER = ("basics", "attention", "permissions", "destination", "review", "complete")
GRANT_KEYS = ("memory", "mail", "calendar", "tasks", "public_sources", "discretionary")
SOURCE_GRANT_KEYS = ("mail", "calendar", "tasks", "public_sources")

_SAFE_TIMEZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*$")
_SAFE_DELIVER = re.compile(r"^[A-Za-z0-9 _:@./+,-]{1,128}$")
_SAFE_LABEL = re.compile(r"^[^\n\r\t\x00]{1,160}$")
_CREDENTIAL_KEYS = frozenset({
    "api_key", "apikey", "api_secret", "secret", "client_secret", "token",
    "access_token", "refresh_token", "password", "passwd", "credential",
    "credentials", "private_key", "authorization", "cookie", "session_key",
})


class OnboardingError(ValueError):
    """A proposed onboarding change is invalid or lacks explicit consent."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "version": 1,
        "status": "not_started",
        "current_step": "basics",
        "created_at": "",
        "updated_at": "",
        "completed_at": "",
        "facts": {},
        "proposals": [],
        "settings": {
            "timezone": "",
            "daily_allowance": DEFAULT_ALLOWANCE,
            "quiet_start_hour": 0,
            "quiet_end_hour": 8,
            "persona": "Hermes",
            "primary_provider": "",
            "primary_model": "",
            "fallback_provider": "",
            "fallback_model": "",
        },
        "grants": {key: False for key in GRANT_KEYS},
        "source_scope": {
            "mail_accounts": [],
            "calendar_accounts": [],
            "calendars": [],
            "task_lists": [],
            "public_interests": [],
        },
        "destination": {"target": "", "status": "unconfigured", "challenge": "",
                        "confirmed_at": ""},
        "capabilities": {},
        "history": [],
        "decided": [],
        # Last values actually written by an onboarding confirmation. This lets
        # a later read tell an applied value (safe to converge on the current
        # native form) from an unsaved in-progress edit (must be preserved).
        "applied": {},
    }


def _scan_credentials(value: Any, path: str = "") -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            dotted = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.strip().lower() in _CREDENTIAL_KEYS and child not in (None, "", [], {}):
                return dotted
            found = _scan_credentials(child, dotted)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _scan_credentials(child, f"{path}[{index}]")
            if found:
                return found
    return None


def _require_int(value: Any, name: str, low: int, high: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OnboardingError(f"{name} must be an integer")
    if value < low or (high is not None and value > high):
        bound = f"at least {low}" if high is None else f"between {low} and {high}"
        raise OnboardingError(f"{name} must be {bound}")
    return value


def _safe_string_list(value: Any, name: str, pattern: re.Pattern[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise OnboardingError(f"{name} must be a list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not pattern.fullmatch(item.strip()):
            raise OnboardingError(f"{name} contains an unsafe or empty entry")
        text = item.strip()
        if text not in out:
            out.append(text)
    return out


def _require_timezone(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OnboardingError("timezone must be a non-empty IANA timezone name")
    text = value.strip()
    if not _SAFE_TIMEZONE.fullmatch(text):
        raise OnboardingError("timezone is not a valid IANA timezone name")
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise OnboardingError(
            "timezone is not a known IANA timezone (e.g. UTC, Europe/London)") from exc
    return text


def validate_settings(patch: Any) -> dict[str, Any]:
    if not isinstance(patch, Mapping):
        raise OnboardingError("settings must be an object")
    found = _scan_credentials(patch)
    if found:
        raise OnboardingError(f"credential-shaped field {found!r} is not accepted")
    allowed = {"timezone", "persona", "daily_allowance", "quiet_start_hour",
               "quiet_end_hour", *ROUTE_SETTING_KEYS}
    unknown = set(patch) - allowed
    if unknown:
        raise OnboardingError(f"unknown setting keys: {', '.join(sorted(map(str, unknown)))}")
    out: dict[str, Any] = {}
    if "timezone" in patch:
        out["timezone"] = _require_timezone(patch["timezone"])
    if "persona" in patch:
        value = patch["persona"]
        if not isinstance(value, str) or not value.strip() or not _SAFE_LABEL.fullmatch(value.strip()):
            raise OnboardingError("persona must be a bounded non-empty string")
        out["persona"] = value.strip()[:64]
    if "daily_allowance" in patch:
        out["daily_allowance"] = _require_int(
            patch["daily_allowance"], "daily_allowance", 0, None)
    for key in ("quiet_start_hour", "quiet_end_hour"):
        if key in patch:
            out[key] = _require_int(patch[key], key, 0, 23)
    route_patch = {key: patch[key] for key in ROUTE_SETTING_KEYS if key in patch}
    if route_patch:
        clean, errors = validate_route_settings(route_patch)
        if errors:
            raise OnboardingError("; ".join(errors))
        out.update(clean)
    return out


def validate_grants(grants: Any, *, explicit_consent: bool) -> dict[str, Any]:
    if not isinstance(grants, Mapping):
        raise OnboardingError("grants must be an object")
    unknown = set(grants) - set(GRANT_KEYS)
    if unknown:
        raise OnboardingError(f"unknown grant keys: {', '.join(sorted(map(str, unknown)))}")
    clean: dict[str, bool] = {}
    for key, value in grants.items():
        if not isinstance(value, bool):
            raise OnboardingError(f"grant {key} must be true or false")
        clean[key] = value
    if any(clean.values()) and not explicit_consent:
        raise OnboardingError("granting a permission requires explicit_consent=true")
    return clean


def validate_destination(value: Any) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _SAFE_DELIVER.fullmatch(value.strip()):
        raise OnboardingError("destination is not a safe deliver target")
    return value.strip()


def validate_fact(key: Any, value: Any) -> tuple[str, str]:
    if not isinstance(key, str) or not _SAFE_LABEL.fullmatch(key.strip()):
        raise OnboardingError("fact key must be a safe short label")
    if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value.strip()):
        raise OnboardingError("fact value must be a safe short string")
    return key.strip()[:80], value.strip()[:400]


def _merge_settings(state: dict[str, Any], patch: Any) -> None:
    state["settings"].update(validate_settings(patch))


def _merge_scope(state: dict[str, Any], patch: Any) -> None:
    if not isinstance(patch, Mapping):
        raise OnboardingError("source_scope must be an object")
    found = _scan_credentials(patch)
    if found:
        raise OnboardingError(f"credential-shaped field {found!r} is not accepted")
    rules = {
        "mail_accounts": re.compile(r"^[A-Za-z0-9._@+-]{1,64}$"),
        "calendar_accounts": re.compile(r"^[A-Za-z0-9._@+-]{1,64}$"),
        "calendars": re.compile(r"^[A-Za-z0-9._@:+/-]{1,128}$"),
        "task_lists": _SAFE_LABEL,
        "public_interests": _SAFE_LABEL,
    }
    for key, pattern in rules.items():
        if key in patch:
            state["source_scope"][key] = _safe_string_list(patch[key], key, pattern)
    unknown = set(patch) - set(rules)
    if unknown:
        raise OnboardingError(f"unknown source_scope keys: {', '.join(sorted(map(str, unknown)))}")


def next_step(state: Mapping[str, Any]) -> str:
    if state.get("status") == "pending_verification":
        # Settings are saved but delivery is NOT confirmed and jobs are NOT
        # armed. This is a distinct, actionable state, never "complete".
        return "delivery_verification"
    if state.get("status") == "ready":
        return "complete"
    settings = state.get("settings") or {}
    destination = state.get("destination") or {}
    decided = set(state.get("decided") or [])
    if not settings.get("timezone"):
        return "basics"
    if not destination.get("target"):
        return "destination"
    # Ask only genuine gaps. A field already decided in this or an existing
    # configuration is never re-asked (no duplicate biography).
    if "setting:daily_allowance" not in decided:
        return "allowance"
    if "setting:quiet_start_hour" not in decided or "setting:quiet_end_hour" not in decided:
        return "quiet"
    if not any(key.startswith("grant:") for key in decided):
        return "permissions"
    return "review"


def next_question(state: Mapping[str, Any]) -> str:
    step = next_step(state)
    if step == "delivery_verification":
        target = str((state.get("destination") or {}).get("target") or "").strip()
        if target:
            command = f"hermes secretary setup --deliver {target}"
            return ("Settings are saved, but delivery is NOT confirmed and jobs are NOT "
                    "armed. In the foreground run `" + command + "` to arm the owned jobs, "
                    "then perform an explicitly authorised native canary. "
                    "`hermes secretary status --json` shows scheduled jobs/next run; "
                    "delivery stays unverified until that canary.")
        return ("Settings are saved, but no delivery destination is confirmed and jobs are "
                "NOT armed. Choose a destination with /secretary setup, then run "
                "`hermes secretary setup --deliver <target>` in the foreground to arm the "
                "owned jobs; `hermes secretary status --json` shows what is scheduled.")
    if step == "basics":
        return ("Which timezone should the secretary use for quiet hours and daily limits? "
                "Give a standard IANA name such as UTC, Europe/London or America/New_York; "
                "a city name alone is not guessed.")
    if step == "destination":
        return ("Which single delivery destination should receive proactive messages? "
                "A destination is not treated as verified until confirmed.")
    if step == "allowance":
        return ("What daily discretionary message ceiling do you want? Any nonnegative "
                "integer; zero means no ordinary discretionary messages. It is a ceiling, "
                "never a target.")
    if step == "quiet":
        return ("Which local quiet hours should defer ordinary messages? Use 24-hour "
                "start/end hours; this is not a pause.")
    if step == "permissions":
        return ("Which standing source permissions, if any, do you explicitly grant? "
                "Memory proposals are not grants, and no source is enabled without your "
                "typed consent.")
    if step == "review":
        return ("Review the preview and confirm. Permissions are only saved with an explicit "
                "typed grant; no schedule is armed by onboarding.")
    return "Onboarding has no further required questions."


class OnboardingService:
    """Profile-scoped state plus validation; no source, model or cron side effects."""

    def __init__(self, ctx: Any = None, *, state: Any = None, config_resolver: Any = None,
                 settings_writer: Any = None) -> None:
        self.ctx = ctx
        self._state_facade = state if state is not None else getattr(ctx, "state", None)
        self._config_resolver = config_resolver
        self._settings_writer = settings_writer

    def _read(self) -> dict[str, Any]:
        raw = None
        if self._state_facade is not None:
            try:
                raw = self._state_facade.get(STATE_KEY)
            except Exception as exc:  # noqa: BLE001
                raise OnboardingError(f"cannot read onboarding state ({type(exc).__name__})") from exc
        if not isinstance(raw, dict):
            state = default_state()
            self._seed_from_effective_config(state)
            return state
        state = default_state()
        for key in state:
            if key in raw:
                if isinstance(state[key], dict) and isinstance(raw[key], dict):
                    state[key].update(copy.deepcopy(raw[key]))
                else:
                    state[key] = copy.deepcopy(raw[key])
        self._seed_from_effective_config(state)
        return state

    @staticmethod
    def _known_fields(loaded: Any) -> set[str]:
        """Fields the user actually chose, never validation-filled factories.

        A real load carries raw-presence provenance. A legacy caller that
        constructs ``LoadedConfig`` directly has none; for that shape only
        non-neutral values are treated as evidence, so factories are not
        mistaken for choices.
        """
        provenance = getattr(loaded, "provenance", None)
        if isinstance(provenance, dict):
            return {str(item) for item in (provenance.get("fields") or [])
                    if isinstance(item, str)}
        try:
            import sys
            from pathlib import Path
            src = str(Path(__file__).resolve().parent / "src")
            if src not in sys.path:
                sys.path.insert(0, src)
            from proactive_pilot.userconfig import (default_config,
                                                    onboarding_provenance,
                                                    raw_value)
            neutral = default_config()
            config = getattr(loaded, "config", {}) or {}
            if onboarding_provenance(config).get("mode") in (
                    "factory_neutral", "neutral_init"):
                return set()
            return {path for path in (
                "timezone", "notification.destination",
                "notification.daily_normal_cap", "notification.quiet_start_hour",
                "notification.quiet_end_hour", "sources.mail", "sources.calendar",
                "sources.tasks", "mail.accounts", "calendar.accounts",
                "calendar.calendars", "tasks.lists", "public_interests",
            ) if raw_value(config, path) != raw_value(neutral, path)}
        except Exception:  # noqa: BLE001 - no provenance is simply no evidence
            return set()

    def _seed_from_effective_config(self, state: dict[str, Any]) -> None:
        """Converge onboarding fields on the CURRENT effective configuration.

        The native/file configuration is the one runtime owner; onboarding state
        is a resumable snapshot, never a shadow preference store. An applied
        value can therefore be refreshed when a native form edit changes the
        effective configuration, while an unsaved in-progress edit is preserved.
        Fields the configuration does not mention (proposed facts, un-applied
        choices and separately granted scopes) are never invented, and
        generated-neutral factories are never mistaken for user decisions.
        """
        try:
            loaded = self._loaded_effective()
        except Exception:  # noqa: BLE001 - no config is simply no seed
            return
        if not loaded.present:
            return
        config = loaded.config
        known = self._known_fields(loaded)
        decided = set(state.get("decided") or [])
        applied = state.get("applied") if isinstance(state.get("applied"), dict) else {}
        applied_decided = set(applied.get("decided") or [])
        complete = state.get("status") in ("pending_verification", "ready")

        def _safe_to_refresh(marker: str, bucket: str, key: str, current: Any) -> bool:
            """True when state does not hold an unsaved edit for this field."""
            if marker not in decided:
                return True  # never presented as a choice: seed from config
            if not current and current != 0:
                return True  # blank state always accepts an explicit config value
            if not applied:
                # Legacy state has no applied snapshot: a completed setup is a
                # stale snapshot; an in-progress one may still be edited.
                return complete
            snapshot = applied.get(bucket) if isinstance(applied.get(bucket), dict) else {}
            return marker in applied_decided and snapshot.get(key) == current

        timezone_name = str(config.get("timezone") or "").strip()
        if timezone_name and "timezone" in known:
            current = state["settings"].get("timezone") or ""
            if _safe_to_refresh("setting:timezone", "settings", "timezone", current):
                state["settings"]["timezone"] = timezone_name
                decided.add("setting:timezone")

        notif = config.get("notification") or {}
        if "notification.destination" in known:
            target = str(notif.get("destination") or "").strip()
            destination = state.setdefault("destination", {})
            current = str(destination.get("target") or "")
            if _safe_to_refresh("destination", "destination", "target", current):
                if target != current:
                    destination.update({"target": target,
                                        "status": "unverified" if target else "unconfigured",
                                        "challenge": "", "confirmed_at": ""})
                elif not target:
                    destination["status"] = "unconfigured"
                elif destination.get("status") == "unconfigured":
                    destination["status"] = "unverified"
                decided.add("destination")

        for marker, field, key in (
                ("setting:daily_allowance", "daily_normal_cap", "daily_allowance"),
                ("setting:quiet_start_hour", "quiet_start_hour", "quiet_start_hour"),
                ("setting:quiet_end_hour", "quiet_end_hour", "quiet_end_hour")):
            if notif.get(field) is not None and f"notification.{field}" in known:
                current = state["settings"].get(key)
                if _safe_to_refresh(marker, "settings", key, current):
                    state["settings"][key] = int(notif[field])
                    decided.add(marker)

        sources = config.get("sources") or {}
        # Onboarding's public grant is the typed public-feeds switch. The two
        # names are intentionally mapped here instead of being written as an
        # unknown ``sources.public_sources`` key that validation would ignore.
        for grant, flag in (("mail", "mail"), ("calendar", "calendar"),
                            ("tasks", "tasks"), ("public_sources", "public_feeds")):
            if f"sources.{flag}" in known:
                current = bool(state["grants"].get(grant))
                if _safe_to_refresh(f"grant:{grant}", "grants", grant, current):
                    state["grants"][grant] = bool(sources.get(flag))
                decided.add(f"grant:{grant}")

        scope = state["source_scope"]
        for field, section, key, path in (
                ("mail_accounts", "mail", "accounts", "mail.accounts"),
                ("calendar_accounts", "calendar", "accounts", "calendar.accounts"),
                ("calendars", "calendar", "calendars", "calendar.calendars"),
                ("task_lists", "tasks", "lists", "tasks.lists")):
            if path in known:
                current = list(scope.get(field) or [])
                if _safe_to_refresh(f"scope:{field}", "source_scope", field, current):
                    # Explicit-empty scope is a decision and must stay empty.
                    scope[field] = list((config.get(section) or {}).get(key) or [])
                decided.add(f"scope:{field}")
        if "public_interests" in known:
            current = list(scope.get("public_interests") or [])
            if _safe_to_refresh("scope:public_interests", "source_scope",
                                "public_interests", current):
                scope["public_interests"] = list(config.get("public_interests") or [])
            decided.add("scope:public_interests")

        # Existing native route controls are operator/user choices. Read only
        # this plugin's four declared route keys; absent keys are not guessed.
        try:
            getter = getattr(self.ctx, "get_config", None)
            if callable(getter):
                missing = object()
                for key in ROUTE_SETTING_KEYS:
                    value = getter(key, missing)
                    if value is not missing and isinstance(value, str):
                        current = state["settings"].get(key) or ""
                        if _safe_to_refresh(f"setting:{key}", "settings", key, current):
                            state["settings"][key] = value.strip()
                        decided.add(f"setting:{key}")
            else:
                from .secretary_models import read_route_settings
                for key, value in read_route_settings(self.ctx).items():
                    if value and not state["settings"].get(key):
                        state["settings"][key] = value
                        decided.add(f"setting:{key}")
        except Exception:  # noqa: BLE001 - routes remain unseeded, not guessed
            pass
        state["decided"] = sorted(decided)[-100:]

    def _write(self, state: dict[str, Any]) -> None:
        if self._state_facade is None:
            raise OnboardingError("durable profile state is unavailable")
        state["updated_at"] = _now()
        try:
            self._state_facade.set(STATE_KEY, state)
        except Exception as exc:  # noqa: BLE001
            raise OnboardingError(f"cannot persist onboarding state ({type(exc).__name__})") from exc

    def _effective_config(self) -> dict[str, Any]:
        if self._config_resolver is not None:
            try:
                loaded = self._config_resolver()
            except Exception as exc:  # noqa: BLE001
                return {"available": False, "error": type(exc).__name__}
            return {"available": True, "source": getattr(loaded, "source", "resolver"),
                    "present": bool(getattr(loaded, "present", False)),
                    "summary": loaded.summary() if hasattr(loaded, "summary") else dict(loaded)}
        if self.ctx is None:
            return {"available": False, "error": "no native context"}
        try:
            import sys
            from pathlib import Path
            src = str(Path(__file__).resolve().parent / "src")
            if src not in sys.path:
                sys.path.insert(0, src)
            from proactive_pilot.userconfig import load_user_config
            loaded = load_user_config(native=self.ctx)
            return {"available": True, "source": loaded.source, "present": loaded.present,
                    "summary": loaded.summary()}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": type(exc).__name__}

    def _capabilities(self) -> dict[str, Any]:
        caps = {
            "slash_command": _has_callable(self.ctx, "register_command"),
            "typed_tool": _has_callable(self.ctx, "register_tool"),
            "durable_state": self._state_facade is not None,
            "pre_llm_context": _has_callable(self.ctx, "register_hook"),
            "gateway_injection": False,
            "destination_live_verification": False,
            "automatic_schedule_arming": False,
        }
        checker = getattr(self.ctx, "_gateway_injection_allowed", None) if self.ctx is not None else None
        if callable(checker):
            try:
                caps["gateway_injection"] = bool(checker())
            except Exception:  # noqa: BLE001
                caps["gateway_injection"] = False
        return caps

    def _model_routes(self) -> dict[str, Any]:
        try:
            import sys
            from pathlib import Path
            src = str(Path(__file__).resolve().parent / "src")
            if src not in sys.path:
                sys.path.insert(0, src)
            from proactive_pilot.native_runtime import bound_model_lane_status
            bound = bound_model_lane_status(self.ctx)
            if bound is not None:
                return bound
            return effective_route_status(self.ctx, strict_active=False)
        except Exception as exc:  # noqa: BLE001
            return {"strict_active": False, "configured_model_calls_allowed": False,
                    "error": type(exc).__name__}

    def inspect(self) -> dict[str, Any]:
        state = self._read()
        return {
            "ok": True,
            "state": state,
            "next_step": next_step(state),
            "next_question": next_question(state),
            "effective_config": self._effective_config(),
            "model_routes": self._model_routes(),
            "capabilities": self._capabilities(),
            "readback": {"record": STATE_KEY, "status": state["status"],
                         "settings": copy.deepcopy(state["settings"]),
                         "grants": copy.deepcopy(state["grants"]),
                         "destination": copy.deepcopy(state["destination"])},
        }

    def propose_facts(self, facts: Any, *, provenance: str = "foreground_memory") -> dict[str, Any]:
        """Accept bounded foreground-memory facts without inventing a memory API.

        Duplicate known facts are skipped (no double biography). A difference
        from an existing fact is marked as a conflict from the data itself, not
        merely because a caller supplied ``conflict: true``.
        """
        if not isinstance(facts, list):
            raise OnboardingError("proposed facts must be a list")
        state = self._read()
        added = []
        skipped = []
        conflicts = []
        for item in facts:
            if not isinstance(item, Mapping):
                raise OnboardingError("each proposed fact must be an object")
            key, value = validate_fact(item.get("key"), item.get("value"))
            existing = (state.get("facts") or {}).get(key)
            if isinstance(existing, Mapping):
                if str(existing.get("value") or "") == value:
                    skipped.append(key)
                    continue
            duplicate = next((p for p in state["proposals"]
                              if p.get("key") == key and p.get("value") == value
                              and not p.get("accepted")), None)
            if duplicate is not None:
                skipped.append(key)
                continue
            previous_values = {
                str(p.get("value") or "") for p in state["proposals"]
                if p.get("key") == key and not p.get("accepted")
            }
            if isinstance(existing, Mapping):
                previous_values.add(str(existing.get("value") or ""))
            conflict = bool(item.get("conflict", False)) or bool(
                previous_values and value not in previous_values)
            proposal = {
                "key": key, "value": value,
                "provenance": str(item.get("provenance") or provenance)[:80],
                "confidence": str(item.get("confidence") or "unspecified")[:32],
                "freshness": str(item.get("freshness") or "unspecified")[:32],
                "conflict": conflict,
                "conflict_reason": ("existing_fact_differs" if conflict else ""),
                "accepted": False, "created_at": _now(),
            }
            state["proposals"].append(proposal)
            added.append(proposal)
            if conflict:
                conflicts.append(key)
        if state["status"] in ("not_started", "cancelled"):
            state["status"] = "in_progress"
            state["created_at"] = state["created_at"] or _now()
        self._write(state)
        return {"ok": True, "added": added, "skipped_existing": sorted(set(skipped)),
                "conflicts": sorted(set(conflicts)),
                "note": ("Proposals are not facts and grant no access. Conflicting facts "
                         "require explicit foreground resolution."),
                "readback": {"record": STATE_KEY, "proposal_count": len(state["proposals"]),
                             "status": state["status"]}}

    def accept_proposals(self, keys: Any) -> dict[str, Any]:
        if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
            raise OnboardingError("accepted proposals must be a list of keys")
        state = self._read()
        accepted = []
        for proposal in state["proposals"]:
            if proposal["key"] in keys and not proposal.get("accepted"):
                state["facts"][proposal["key"]] = {
                    "value": proposal["value"], "provenance": proposal["provenance"],
                    "confidence": proposal["confidence"], "freshness": proposal["freshness"],
                    "updated_at": _now(),
                }
                proposal["accepted"] = True
                accepted.append(proposal["key"])
        self._write(state)
        return {"ok": True, "accepted": accepted,
                "readback": {"record": STATE_KEY, "facts": copy.deepcopy(state["facts"])}}

    def update(self, *, settings: Any = None, grants: Any = None, source_scope: Any = None,
               destination: Any = None, explicit_consent: bool = False,
               step: str | None = None) -> dict[str, Any]:
        state = self._read()
        changed: list[str] = []
        decided = set(state.get("decided") or [])
        if settings is not None:
            clean_settings = validate_settings(settings)
            candidate = dict(state["settings"])
            candidate.update(clean_settings)
            _clean_route, route_errors = validate_route_settings(candidate)
            if route_errors:
                raise OnboardingError("; ".join(route_errors))
            state["settings"].update(clean_settings)
            decided.update(f"setting:{key}" for key in clean_settings)
            changed.append("settings")
        if grants is not None:
            clean = validate_grants(grants, explicit_consent=explicit_consent)
            state["grants"].update(clean)
            decided.update(f"grant:{key}" for key in clean)
            changed.append("grants")
        if source_scope is not None:
            if not explicit_consent and any((state["grants"].get(key) for key in SOURCE_GRANT_KEYS)):
                raise OnboardingError("changing source scope requires explicit_consent=true")
            _merge_scope(state, source_scope)
            decided.update(f"scope:{key}" for key in source_scope)
            changed.append("source_scope")
        if destination is not None:
            target = validate_destination(destination)
            state["destination"]["target"] = target
            state["destination"]["status"] = "unverified" if target else "unconfigured"
            state["destination"]["challenge"] = ""
            state["destination"]["confirmed_at"] = ""
            decided.add("destination")
            changed.append("destination")
        state["decided"] = sorted(decided)[-100:]
        if step is not None:
            if step not in STEP_ORDER:
                raise OnboardingError(f"unknown onboarding step {step!r}")
            state["current_step"] = step
            changed.append("current_step")
        if state["status"] in ("not_started", "cancelled"):
            state["status"] = "in_progress"
            state["created_at"] = state["created_at"] or _now()
        state["current_step"] = next_step(state)
        state["history"].append({"at": _now(), "changed": changed})
        state["history"] = state["history"][-50:]
        self._write(state)
        return {"ok": True, "changed": changed,
                "readback": {"record": STATE_KEY, "settings": copy.deepcopy(state["settings"]),
                             "grants": copy.deepcopy(state["grants"]),
                             "source_scope": copy.deepcopy(state["source_scope"]),
                             "destination": copy.deepcopy(state["destination"]),
                             "next_step": state["current_step"]}}

    def request_destination_verification(self) -> dict[str, Any]:
        state = self._read()
        target = state["destination"].get("target")
        if not target:
            raise OnboardingError("set a destination before requesting verification")
        challenge = f"verify-{_now().replace(':', '').replace('-', '')[:15]}"
        state["destination"]["status"] = "pending"
        state["destination"]["challenge"] = challenge
        self._write(state)
        return {"ok": True, "status": "pending", "challenge": challenge,
                "note": ("No live delivery receipt is available to this plugin. A foreground "
                         "confirmation is recorded as user_confirmed, never as live-verified."),
                "readback": {"record": STATE_KEY, "destination": copy.deepcopy(state["destination"])}}

    def confirm_destination(self, challenge: Any, *, user_confirmed: bool = False) -> dict[str, Any]:
        state = self._read()
        expected = state["destination"].get("challenge")
        if not user_confirmed:
            raise OnboardingError("destination confirmation requires user_confirmed=true")
        if not expected or challenge != expected:
            raise OnboardingError("destination challenge does not match")
        state["destination"]["status"] = "user_confirmed"
        state["destination"]["confirmed_at"] = _now()
        state["destination"]["challenge"] = ""
        self._write(state)
        return {"ok": True, "status": "user_confirmed",
                "note": "Not a live delivery verification.",
                "readback": {"record": STATE_KEY, "destination": copy.deepcopy(state["destination"])}}

    def preview(self) -> dict[str, Any]:
        state = self._read()
        planned = {
            "timezone": state["settings"].get("timezone") or None,
            "notification.daily_normal_cap": state["settings"].get("daily_allowance"),
            "notification.quiet_start_hour": state["settings"].get("quiet_start_hour"),
            "notification.quiet_end_hour": state["settings"].get("quiet_end_hour"),
            "notification.destination": state["destination"].get("target") or None,
            "sources": {key: bool(state["grants"].get(key)) for key in SOURCE_GRANT_KEYS},
            "source_scope": copy.deepcopy(state["source_scope"]),
            "model_route": {key: state["settings"].get(key, "") for key in ROUTE_SETTING_KEYS},
        }
        blockers = []
        if not state["settings"].get("timezone"):
            blockers.append("timezone")
        if not state["destination"].get("target"):
            blockers.append("destination")
        destination_status = state["destination"].get("status")
        verification = []
        if destination_status in ("pending", "unconfigured", "unverified", "user_confirmed"):
            verification.append("destination_verification_pending")
        _clean, route_errors = validate_route_settings(state["settings"])
        blockers.extend(f"model_route: {error}" for error in route_errors)
        return {"ok": True, "status": state["status"], "planned_effective_settings": planned,
                "blockers": blockers, "would_apply": not blockers,
                "pending_verification": verification,
                "destination_status": destination_status,
                "live_destination_verified": False,
                "note": ("Preview only: no settings, grants, jobs or sources were changed. "
                         "Destination verification is pending; onboarding never auto-arms jobs "
                         "or claims live delivery."),
                "readback": {"record": STATE_KEY, "planned": planned, "blockers": blockers}}

    def _current_raw_user_config(self) -> dict[str, Any]:
        if self.ctx is None or not _has_callable(self.ctx, "get_config"):
            raise OnboardingError("native settings are unavailable")
        raw = self.ctx.get_config("user_config")
        if raw is None or raw == "":
            return {}
        if not isinstance(raw, dict):
            raise OnboardingError("native user_config is present but not an object")
        return copy.deepcopy(raw)

    def _loaded_effective(self) -> Any:
        if self._config_resolver is not None:
            try:
                return self._config_resolver()
            except Exception as exc:  # noqa: BLE001
                raise OnboardingError(
                    f"cannot resolve effective configuration ({type(exc).__name__})") from exc
        if self.ctx is None:
            raise OnboardingError("native settings are unavailable")
        import sys
        from pathlib import Path
        src = str(Path(__file__).resolve().parent / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from proactive_pilot.userconfig import load_user_config
        try:
            return load_user_config(native=self.ctx)
        except Exception as exc:  # noqa: BLE001
            raise OnboardingError(
                f"effective configuration is invalid ({type(exc).__name__})") from exc

    def _apply_settings(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Apply only decided fields through the one canonical config owner.

        The effective validated config (native opaque + flat form + defaults) is
        the base, so an onboarding edit preserves every existing account, alias,
        grant and cap. The merged record is validated once, written to the same
        native ``user_config`` value, then ordinary fields are projected through
        ``project_native_settings``. No cloned precedence exists here.
        """
        import sys
        from pathlib import Path
        src = str(Path(__file__).resolve().parent / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        from proactive_pilot.userconfig import (native_flat_changes,
                                                project_native_settings,
                                                validate_config)

        loaded = self._loaded_effective()
        config = copy.deepcopy(loaded.config)
        settings = state["settings"]
        decided = set(state.get("decided") or [])
        # Record only fields this onboarding write actually chose. This keeps
        # generated-neutral factory values suppressed on later loads while real
        # explicit choices (including UTC/5/0/8) stay known.
        chosen_paths: set[str] = set()
        field_markers = {
            "setting:timezone": "timezone",
            "setting:daily_allowance": "notification.daily_normal_cap",
            "setting:quiet_start_hour": "notification.quiet_start_hour",
            "setting:quiet_end_hour": "notification.quiet_end_hour",
            "destination": "notification.destination",
            "scope:mail_accounts": "mail.accounts",
            "scope:calendar_accounts": "calendar.accounts",
            "scope:calendars": "calendar.calendars",
            "scope:task_lists": "tasks.lists",
            "scope:public_interests": "public_interests",
        }
        for marker, path in field_markers.items():
            if marker in decided:
                chosen_paths.add(path)
        source_flags = {"mail": "mail", "calendar": "calendar",
                        "tasks": "tasks", "public_sources": "public_feeds"}
        for key in SOURCE_GRANT_KEYS:
            if f"grant:{key}" in decided:
                chosen_paths.add(f"sources.{source_flags[key]}")
        if chosen_paths:
            advanced = config.setdefault("advanced", {})
            if isinstance(advanced, dict):
                marker = advanced.get("onboarding_provenance")
                existing_marker = marker if isinstance(marker, dict) else None
                provenance = getattr(loaded, "provenance", None) or {}
                if existing_marker is not None:
                    marker = dict(existing_marker)
                elif provenance.get("mode") == "explicit":
                    marker = {"schema": 1, "neutral_init": False,
                              "known_fields": list(provenance.get("fields") or [])}
                else:
                    from proactive_pilot.userconfig import raw_value
                    marker = {
                        "schema": 1, "neutral_init": True, "known_fields": [],
                        "baseline": {path: copy.deepcopy(raw_value(config, path))
                                     for path in (
                                         "timezone", "notification.destination",
                                         "notification.daily_normal_cap",
                                         "notification.quiet_start_hour",
                                         "notification.quiet_end_hour",
                                         "sources.public_feeds", "sources.obligations",
                                         "sources.mail", "sources.calendar",
                                         "sources.tasks", "mail.accounts",
                                         "calendar.accounts", "calendar.calendars",
                                         "tasks.lists", "public_interests")},
                    }
                known = {str(item) for item in (marker.get("known_fields") or [])
                         if isinstance(item, str)}
                known.update(chosen_paths)
                marker["known_fields"] = sorted(known)[-100:]
                advanced["onboarding_provenance"] = marker
        notification = config.setdefault("notification", {})
        for key, field in (("daily_allowance", "daily_normal_cap"),
                           ("quiet_start_hour", "quiet_start_hour"),
                           ("quiet_end_hour", "quiet_end_hour")):
            if f"setting:{key}" in decided:
                notification[field] = settings[key]
        if "destination" in decided and state["destination"].get("target"):
            notification["destination"] = state["destination"]["target"]
        if "setting:timezone" in decided:
            config["timezone"] = settings["timezone"]
        sources = config.setdefault("sources", {})
        for key in SOURCE_GRANT_KEYS:
            if f"grant:{key}" in decided:
                sources[source_flags[key]] = bool(state["grants"].get(key))
        scope = state["source_scope"]
        for section, field, decided_key, scope_key in (
                ("mail", "accounts", "scope:mail_accounts", "mail_accounts"),
                ("calendar", "accounts", "scope:calendar_accounts", "calendar_accounts"),
                ("calendar", "calendars", "scope:calendars", "calendars"),
                ("tasks", "lists", "scope:task_lists", "task_lists")):
            if decided_key in decided:
                node = config.setdefault(section, {})
                if not isinstance(node, dict):
                    raise OnboardingError(f"native {section} settings are malformed")
                node[field] = list(scope[scope_key])
        if "scope:public_interests" in decided:
            config["public_interests"] = list(scope["public_interests"])

        validated = validate_config(config)
        persisted = {key: value for key, value in validated.items()
                     if not str(key).startswith("_")}
        writer = self._settings_writer
        if writer is None and not _has_callable(self.ctx, "set_config"):
            raise OnboardingError("native settings writer is unavailable")

        def _same_user_config(value: Any) -> bool:
            if isinstance(value, Mapping):
                return dict(value) == persisted
            if isinstance(value, str) and value.strip():
                try:
                    return json.loads(value) == persisted
                except ValueError:
                    return False
            return False

        getter = getattr(self.ctx, "get_config", None)
        missing = object()
        current_user_config = getter("user_config", missing) if callable(getter) else missing
        user_config_changed = not (current_user_config is not missing
                                   and _same_user_config(current_user_config))
        projection_changes = (native_flat_changes(self.ctx, validated)
                              if _has_callable(self.ctx, "set_config") else {})
        route_writes: dict[str, str] = {}
        if callable(getter) and any(f"setting:{key}" in decided
                                    for key in ROUTE_SETTING_KEYS):
            for key in ROUTE_SETTING_KEYS:
                desired = str(settings.get(key) or "")
                current = getter(key, missing)
                if current is missing and not desired:
                    continue
                if current is not missing and isinstance(current, str) \
                        and current.strip() == desired:
                    continue
                route_writes[key] = desired

        projected: list[str] = []
        route_written: list[str] = []
        backup = None
        if user_config_changed or projection_changes or route_writes:
            try:
                from proactive_pilot import config_backup
                from . import paths as plugin_paths
                backup = config_backup.create_backup(
                    plugin_paths.data_root(), native=self.ctx,
                    state=self._state_facade, reason="onboarding apply settings")
            except Exception as exc:  # noqa: BLE001 - never write without backup
                raise OnboardingError(
                    "settings backup failed; nothing was changed "
                    f"({type(exc).__name__})") from exc
            if user_config_changed:
                if writer is not None:
                    writer(copy.deepcopy(persisted))
                else:
                    self.ctx.set_config("user_config", persisted)
            if projection_changes:
                projected = project_native_settings(self.ctx, validated)
            for key, value in route_writes.items():
                self.ctx.set_config(key, value)
                route_written.append(key)

        after = {
            "record": (f"plugins.entries.{getattr(self.ctx, 'plugin_id', 'hermes-proactive-secretary')}"
                       ".settings.user_config"),
            "timezone": persisted.get("timezone"),
            "notification.daily_normal_cap": persisted["notification"]["daily_normal_cap"],
            "notification.destination": persisted["notification"].get("destination", ""),
            "mail_accounts": list(persisted["mail"]["accounts"]),
            "calendar_accounts": list(persisted["calendar"]["accounts"]),
            "task_lists": list(persisted["tasks"]["lists"]),
            "sources": copy.deepcopy(persisted.get("sources", {})),
            "projected_native_fields": projected,
            "projected_model_fields": route_written,
            "effective_source": loaded.source,
            "backup": backup,
            "changed": bool(user_config_changed or projection_changes or route_writes),
        }
        return after

    def confirm(self, *, user_confirmed: bool = False, apply_settings: bool = False) -> dict[str, Any]:
        if not user_confirmed:
            raise OnboardingError("onboarding confirmation requires user_confirmed=true")
        state = self._read()
        blockers = self.preview()["blockers"]
        if blockers:
            raise OnboardingError(f"onboarding is incomplete: {', '.join(blockers)}")
        config_readback = None
        if apply_settings:
            config_readback = self._apply_settings(state)
            state["applied"] = {
                "at": _now(),
                "decided": sorted(state.get("decided") or []),
                "settings": copy.deepcopy(state["settings"]),
                "grants": copy.deepcopy(state["grants"]),
                "source_scope": copy.deepcopy(state["source_scope"]),
                "destination": (state.get("destination") or {}).get("target", ""),
            }
        state["status"] = "pending_verification"
        state["current_step"] = "delivery_verification"
        state["completed_at"] = _now()
        state["history"].append({"at": _now(), "changed": ["confirmed"],
                                 "applied_settings": bool(apply_settings)})
        state["history"] = state["history"][-50:]
        self._write(state)
        return {"ok": True, "status": "pending_verification",
                "applied_settings": bool(apply_settings),
                "schedule_armed": False, "grants_changed_by_confirmation": False,
                "live_destination_verified": False,
                "note": ("Configuration is saved. Delivery verification is still pending: "
                         "onboarding cannot verify the platform destination or arm jobs. "
                         "Next run `hermes secretary setup --deliver "
                         + str((state.get("destination") or {}).get("target") or "<target>")
                         + "` in the foreground, then an explicitly authorised native "
                         "canary; `hermes secretary status --json` shows jobs and next run."),
                "config_readback": config_readback,
                "readback": {"record": STATE_KEY, "status": state["status"],
                             "settings": copy.deepcopy(state["settings"]),
                             "grants": copy.deepcopy(state["grants"]),
                             "destination": copy.deepcopy(state["destination"])}}

    def cancel(self) -> dict[str, Any]:
        state = self._read()
        state["status"] = "cancelled"
        state["current_step"] = next_step(state)
        state["history"].append({"at": _now(), "changed": ["cancelled"]})
        state["history"] = state["history"][-50:]
        self._write(state)
        return {"ok": True, "status": "cancelled",
                "note": "Progress is retained and can be resumed with /secretary setup.",
                "readback": {"record": STATE_KEY, "status": state["status"],
                             "settings": copy.deepcopy(state["settings"])}}

    def dispatch(self, action: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        action = str(action or "inspect").strip().lower()
        if action == "inspect":
            return self.inspect()
        if action in ("propose", "propose_facts"):
            return self.propose_facts(payload.get("facts"), provenance=str(
                payload.get("provenance") or "foreground_memory"))
        if action in ("accept", "accept_proposals"):
            return self.accept_proposals(payload.get("keys"))
        if action == "update":
            return self.update(settings=payload.get("settings"), grants=payload.get("grants"),
                               source_scope=payload.get("source_scope"),
                               destination=payload.get("destination"),
                               explicit_consent=bool(payload.get("explicit_consent")),
                               step=payload.get("step"))
        if action == "preview":
            return self.preview()
        if action == "request_verification":
            return self.request_destination_verification()
        if action == "confirm_destination":
            return self.confirm_destination(payload.get("challenge"),
                                            user_confirmed=bool(payload.get("user_confirmed")))
        if action == "confirm":
            return self.confirm(user_confirmed=bool(payload.get("user_confirmed")),
                                apply_settings=bool(payload.get("apply_settings")))
        if action == "cancel":
            return self.cancel()
        raise OnboardingError(f"unknown onboarding action {action!r}")


def _has_callable(obj: Any, name: str) -> bool:
    return obj is not None and callable(getattr(obj, name, None))


TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [
            "inspect", "propose", "accept", "update", "preview",
            "request_verification", "confirm_destination", "confirm", "cancel",
        ]},
        "facts": {"type": "array", "items": {"type": "object"}},
        "keys": {"type": "array", "items": {"type": "string"}},
        "settings": {"type": "object"},
        "grants": {"type": "object"},
        "source_scope": {"type": "object"},
        "destination": {"type": "string"},
        "explicit_consent": {"type": "boolean"},
        "user_confirmed": {"type": "boolean"},
        "apply_settings": {"type": "boolean"},
        "challenge": {"type": "string"},
        "step": {"type": "string"},
    },
    "required": ["action"],
    "additionalProperties": False,
}


def tool_handler(service: OnboardingService):
    def handler(args: Mapping[str, Any], **_kwargs: Any) -> str:
        import json
        try:
            return json.dumps(service.dispatch(str(args.get("action") or "inspect"), args),
                              ensure_ascii=False, sort_keys=True)
        except OnboardingError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": f"onboarding failed ({type(exc).__name__})"},
                              ensure_ascii=False, sort_keys=True)
    return handler


def is_complete(service: OnboardingService) -> bool:
    return service._read().get("status") == "pending_verification"
