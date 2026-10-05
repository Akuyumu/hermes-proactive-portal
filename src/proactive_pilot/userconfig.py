"""Versioned, validated per-user configuration for the proactive secretary.

This module is the generic base configuration layer. It is deliberately
separate from:

* the shipped policy defaults (:mod:`proactive_pilot.policy`) -- those stay the
  neutral runtime defaults;
* learned preferences (:mod:`proactive_pilot.feedback`) -- mutable, revised by
  feedback, never configuration;
* durable runtime state (``var/``) -- mutable, never configuration.

Design rules:

* The configuration schema is versioned. Unknown, newer or corrupt versions
  fail CLOSED with an actionable remedy; a stale (older) version requires an
  explicit migration and is never rewritten silently.
* Credential VALUES are not configuration. Any credential-shaped key with a
  value is rejected, so no token/secret can be smuggled into the file.
* Nothing here reads the network, a connector, a credential store or a live
  profile. Native plugin settings are preferred through a duck-typed accessor
  (``PluginContext.get_config``) when supplied; otherwise an explicit file path
  is read.

The public API is intentionally small: :func:`default_config`,
:func:`validate_config`, :func:`load_user_config`, :func:`migrate_legacy`,
:func:`policy_overrides`, :func:`apply_user_config`.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

CONFIG_SCHEMA = 1
CONFIG_VERSION = 1
DEFAULT_TIMEZONE = "UTC"
NATIVE_PLUGIN_ID = "hermes-proactive-secretary"
NATIVE_SETTINGS_KEY = "user_config"


class ConfigError(ValueError):
    """The user configuration is missing, corrupt, unknown or unsafe."""

    def __init__(self, message: str, *, remedy: str = "") -> None:
        self.remedy = remedy
        text = message if not remedy else f"{message} ({remedy})"
        super().__init__(text)


# Keys that must never carry a credential value in configuration. Matching is
# on the leaf key name, case-insensitively, at any depth. Credential POINTERS
# (e.g. an env var name) are also refused: this file is not a credential store.
_CREDENTIAL_KEYS = frozenset({
    "api_key", "apikey", "api_secret", "secret", "client_secret", "clientsecret",
    "token", "access_token", "refresh_token", "id_token", "bearer",
    "password", "passwd", "pass", "credential", "credentials", "private_key",
    "authorization", "auth", "oauth", "cookie", "session_key",
})

# Destination is passed as a fixed ``--deliver`` argv token, so it is bounded to
# characters that cannot become a shell/argv injection.
_SAFE_DELIVER = re.compile(r"^[A-Za-z0-9 _:@./+,-]{1,128}$")
_SAFE_ACCOUNT = re.compile(r"^[A-Za-z0-9._@+-]{1,64}$")
_SAFE_CALENDAR = re.compile(r"^[A-Za-z0-9._@:+/-]{1,128}$")
# Human-facing labels (task list names, public interests): bounded, no control
# characters, but spaces and ordinary punctuation are allowed.
_SAFE_LABEL = re.compile(r"^[^\n\r\t\x00]{1,160}$")
_SAFE_TIMEZONE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*$")

_SECTIONS = (
    "schema", "version", "timezone", "notification", "sources", "mail",
    "calendar", "tasks", "public_interests", "public_sources", "adaptive",
    "bounds", "adapters", "advanced",
)


def default_config() -> dict[str, Any]:
    """A neutral, generic configuration: no owner identifiers, no accounts."""
    return {
        "schema": CONFIG_SCHEMA,
        "version": CONFIG_VERSION,
        "timezone": DEFAULT_TIMEZONE,
        "notification": {
            "persona": "Hermes",
            "notify_address": "the user",
            "destination": "",
            "quiet_start_hour": 0,
            "quiet_end_hour": 8,
            "daily_normal_cap": 3,
            "cooldown_minutes": 30,
        },
        "sources": {
            "public_feeds": False,
            "obligations": False,
            "mail": False,
            "calendar": False,
            "tasks": False,
        },
        "mail": {"accounts": [], "aliases": {}},
        "calendar": {"accounts": [], "calendars": []},
        "tasks": {"accounts": [], "lists": [], "adapter": ""},
        "public_interests": [],
        "public_sources": [],
        "adaptive": {
            "mode": "disabled",
            "privacy_review": False,
            "inferred_goals": False,
        },
        "bounds": {
            "collector_budget_seconds": 60,
            "max_candidates_per_kind": 60,
            "feed_max_sources_per_run": 2,
        },
        "adapters": {
            "productivity_secretary": False,
            "gws_calendar": False,
        },
    }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping, got {type(value).__name__}",
                          remedy=f"fix the {name!r} section or delete user_config.json")
    return value


def _scan_credentials(value: Any, path: str = "") -> str | None:
    """Return the dotted path of the first credential-shaped key with a value."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.strip().lower() in _CREDENTIAL_KEYS and child not in (None, "", {}, []):
                return key_path
            found = _scan_credentials(child, key_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _scan_credentials(child, f"{path}[{index}]")
            if found:
                return found
    return None


def _validate_str_list(raw: Any, name: str, pattern: re.Pattern[str]) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"{name} must be a list", remedy=f"use a JSON array for {name!r}")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{name} entries must be non-empty strings")
        text = item.strip()
        if not pattern.fullmatch(text):
            raise ConfigError(f"{name} entry {text!r} is not a safe explicit name")
        if text not in out:
            out.append(text)
    return out


def _bounded_int(raw: Any, name: str, low: int, high: int, default: int) -> int:
    if raw is None:
        return default
    if not _is_int(raw):
        raise ConfigError(f"{name} must be an integer")
    if not (low <= raw <= high):
        raise ConfigError(f"{name} must be between {low} and {high}, got {raw}")
    return raw


def _bounded_bool(raw: Any, name: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ConfigError(f"{name} must be true or false")
    return raw


def validate_config(raw: Any) -> dict[str, Any]:
    """Validate and normalise a user configuration, failing closed.

    Unknown top-level keys are reported as warnings on the returned config's
    ``_warnings`` list rather than silently accepted, but the version/schema and
    every known field are enforced strictly.
    """
    if raw is None:
        return default_config()
    if not isinstance(raw, dict):
        raise ConfigError("user configuration must be a JSON object",
                          remedy="rewrite user_config.json as a JSON object")

    found = _scan_credentials(raw)
    if found:
        raise ConfigError(
            f"credential-shaped key {found!r} is not allowed in configuration",
            remedy="remove it and use the native Hermes credential/setup flow",
        )

    schema = raw.get("schema", CONFIG_SCHEMA)
    if not _is_int(schema) or schema != CONFIG_SCHEMA:
        raise ConfigError(
            f"unsupported config schema {schema!r} (expected {CONFIG_SCHEMA})",
            remedy="run `hermes secretary configure migrate` or fix the schema field",
        )
    version = raw.get("version", CONFIG_VERSION)
    if not _is_int(version) or version < 0:
        raise ConfigError(
            f"corrupt config version {version!r}",
            remedy="run `hermes secretary configure migrate --from-policy <policy.json>`",
        )
    if version > CONFIG_VERSION:
        raise ConfigError(
            f"config version {version} is newer than this plugin supports ({CONFIG_VERSION})",
            remedy="update the plugin; refusing to guess a future schema",
        )

    config = default_config()
    warnings: list[str] = []
    for key in raw:
        if key not in _SECTIONS:
            warnings.append(f"ignoring unknown config key {key!r}")

    timezone = raw.get("timezone")
    if timezone is not None:
        if not isinstance(timezone, str) or not _SAFE_TIMEZONE.fullmatch(timezone.strip()):
            raise ConfigError(
                f"timezone {timezone!r} is not a valid IANA timezone name",
                remedy="use e.g. \"UTC\" or \"Europe/London\", or omit it for the neutral default",
            )
        config["timezone"] = timezone.strip()

    notif = _require_mapping(raw.get("notification") or {}, "notification")
    base_notif = config["notification"]
    if "persona" in notif:
        persona = notif["persona"]
        if not isinstance(persona, str) or not persona.strip():
            raise ConfigError("notification.persona must be a non-empty string")
        base_notif["persona"] = persona.strip()[:64]
    if "notify_address" in notif:
        address = notif["notify_address"]
        if not isinstance(address, str) or not address.strip():
            raise ConfigError("notification.notify_address must be a non-empty string")
        base_notif["notify_address"] = address.strip()[:64]
    if "destination" in notif:
        dest = notif["destination"]
        if dest is None:
            dest = ""
        if not isinstance(dest, str) or (dest and not _SAFE_DELIVER.fullmatch(dest.strip())):
            raise ConfigError(
                f"notification.destination {dest!r} is not a safe deliver target",
                remedy="use \"local\", a chat target, or leave it empty",
            )
        base_notif["destination"] = dest.strip()
    base_notif["quiet_start_hour"] = _bounded_int(
        notif.get("quiet_start_hour"), "notification.quiet_start_hour", 0, 23,
        base_notif["quiet_start_hour"])
    base_notif["quiet_end_hour"] = _bounded_int(
        notif.get("quiet_end_hour"), "notification.quiet_end_hour", 0, 23,
        base_notif["quiet_end_hour"])
    base_notif["daily_normal_cap"] = _bounded_int(
        notif.get("daily_normal_cap"), "notification.daily_normal_cap", 0, 50,
        base_notif["daily_normal_cap"])
    base_notif["cooldown_minutes"] = _bounded_int(
        notif.get("cooldown_minutes"), "notification.cooldown_minutes", 0, 1440,
        base_notif["cooldown_minutes"])

    sources = _require_mapping(raw.get("sources") or {}, "sources")
    for key in ("public_feeds", "obligations", "mail", "calendar", "tasks"):
        config["sources"][key] = _bounded_bool(sources.get(key), f"sources.{key}",
                                               config["sources"][key])

    mail = _require_mapping(raw.get("mail") or {}, "mail")
    config["mail"]["accounts"] = _validate_str_list(
        mail.get("accounts"), "mail.accounts", _SAFE_ACCOUNT)
    aliases = _require_mapping(mail.get("aliases") or {}, "mail.aliases")
    clean_aliases: dict[str, str] = {}
    for key, value in aliases.items():
        if not isinstance(key, str) or not _SAFE_ACCOUNT.fullmatch(key.strip()):
            raise ConfigError(f"mail.aliases key {key!r} is not a safe account name")
        if not isinstance(value, str) or not _SAFE_ACCOUNT.fullmatch(value.strip()):
            raise ConfigError(f"mail.aliases[{key!r}] is not a safe account name")
        clean_aliases[key.strip()] = value.strip()
    config["mail"]["aliases"] = clean_aliases

    calendar = _require_mapping(raw.get("calendar") or {}, "calendar")
    config["calendar"]["accounts"] = _validate_str_list(
        calendar.get("accounts"), "calendar.accounts", _SAFE_ACCOUNT)
    config["calendar"]["calendars"] = _validate_str_list(
        calendar.get("calendars"), "calendar.calendars", _SAFE_CALENDAR)

    tasks = _require_mapping(raw.get("tasks") or {}, "tasks")
    config["tasks"]["accounts"] = _validate_str_list(
        tasks.get("accounts", calendar.get("accounts")), "tasks.accounts", _SAFE_ACCOUNT)
    if "accounts" not in tasks and calendar.get("accounts"):
        warnings.append("legacy tasks.accounts omitted: copied explicit calendar accounts; "
                        "set tasks.accounts explicitly to separate the scopes")
    config["tasks"]["lists"] = _validate_str_list(
        tasks.get("lists"), "tasks.lists", _SAFE_LABEL)
    adapter = tasks.get("adapter", "")
    if adapter is None:
        adapter = ""
    if not isinstance(adapter, str) or (adapter and not _SAFE_ACCOUNT.fullmatch(adapter.strip())):
        raise ConfigError("tasks.adapter must be empty or a safe adapter name")
    config["tasks"]["adapter"] = adapter.strip()
    if config["tasks"]["adapter"] not in {"", "tasks", "productivity_secretary"}:
        raise ConfigError("unsupported tasks.adapter; use empty, tasks or productivity_secretary")

    config["public_interests"] = _validate_str_list(
        raw.get("public_interests"), "public_interests", _SAFE_LABEL)
    config["public_sources"] = _validate_public_sources(raw.get("public_sources"))

    adaptive = _require_mapping(raw.get("adaptive") or {}, "adaptive")
    mode = adaptive.get("mode", "disabled")
    if mode not in {"disabled", "shadow", "active"}:
        raise ConfigError("adaptive.mode must be disabled, shadow or active")
    config["adaptive"]["mode"] = mode
    config["adaptive"]["privacy_review"] = _bounded_bool(
        adaptive.get("privacy_review"), "adaptive.privacy_review", False)
    config["adaptive"]["inferred_goals"] = _bounded_bool(
        adaptive.get("inferred_goals"), "adaptive.inferred_goals", False)
    if config["adaptive"]["inferred_goals"] and not config["adaptive"]["privacy_review"]:
        warnings.append(
            "adaptive.inferred_goals is enabled without adaptive.privacy_review; "
            "the structural privacy boundary still runs but the optional semantic "
            "review stays off")

    bounds = _require_mapping(raw.get("bounds") or {}, "bounds")
    config["bounds"]["collector_budget_seconds"] = _bounded_int(
        bounds.get("collector_budget_seconds"), "bounds.collector_budget_seconds",
        5, 600, config["bounds"]["collector_budget_seconds"])
    config["bounds"]["max_candidates_per_kind"] = _bounded_int(
        bounds.get("max_candidates_per_kind"), "bounds.max_candidates_per_kind",
        1, 500, config["bounds"]["max_candidates_per_kind"])
    config["bounds"]["feed_max_sources_per_run"] = _bounded_int(
        bounds.get("feed_max_sources_per_run"), "bounds.feed_max_sources_per_run",
        0, 20, config["bounds"]["feed_max_sources_per_run"])

    adapters = _require_mapping(raw.get("adapters") or {}, "adapters")
    config["adapters"]["productivity_secretary"] = _bounded_bool(
        adapters.get("productivity_secretary"), "adapters.productivity_secretary",
        config["adapters"]["productivity_secretary"])
    config["adapters"]["gws_calendar"] = _bounded_bool(
        adapters.get("gws_calendar"), "adapters.gws_calendar",
        config["adapters"]["gws_calendar"])

    # Explicit, reported compatibility passthrough of the remaining effective
    # legacy policy fields (migration parity). This is the ONLY place arbitrary
    # legacy policy scalars are retained; unknown keys are never silently dropped
    # because the caller can inspect this mapping and the migration report.
    advanced = _require_mapping(raw.get("advanced") or {}, "advanced")
    legacy_policy = advanced.get("legacy_policy")
    if legacy_policy is None:
        legacy_policy = {}
    legacy_policy = _require_mapping(legacy_policy, "advanced.legacy_policy")
    config["advanced"] = {"legacy_policy": copy.deepcopy(dict(legacy_policy))}
    discovery = advanced.get("legacy_account_discovery", [])
    if not isinstance(discovery, list) or any(
            kind not in {"calendar", "tasks"} for kind in discovery):
        raise ConfigError("advanced.legacy_account_discovery must list calendar/tasks only")
    if discovery:
        config["advanced"]["legacy_account_discovery"] = list(dict.fromkeys(discovery))

    config["version"] = version
    config["_warnings"] = warnings
    config["_explicit_version"] = version
    return config


def _validate_public_sources(raw: Any) -> list[dict[str, Any]]:
    """Public source specs are structural only; credentials are rejected above."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("public_sources must be a list")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError("public_sources entries must be objects")
        entry: dict[str, Any] = {}
        for key in ("kind", "url", "provenance", "interest"):
            if key in item:
                if not isinstance(item[key], str):
                    raise ConfigError(f"public_sources[].{key} must be a string")
                entry[key] = item[key][:500]
        if "cadence_minutes" in item:
            entry["cadence_minutes"] = _bounded_int(
                item["cadence_minutes"], "public_sources[].cadence_minutes", 1, 10080, 60)
        if not entry.get("kind") or not entry.get("url"):
            raise ConfigError("public_sources entries require 'kind' and 'url'")
        out.append(entry)
    return out


def policy_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Map a validated user config onto explicit policy keys.

    Only the keys the configuration layer owns are returned; nothing about the
    evaluator, cron, credentials or native provider/search config is duplicated.
    """
    notif = config["notification"]
    # Start from the explicit, reported compatibility passthrough so EVERY
    # retained effective legacy policy field survives; the typed section mapping
    # below then takes precedence over it for the keys the config layer owns.
    overrides: dict[str, Any] = {}
    advanced = config.get("advanced") or {}
    if isinstance(advanced, dict) and isinstance(advanced.get("legacy_policy"), dict):
        for key, value in advanced["legacy_policy"].items():
            overrides[key] = copy.deepcopy(value)
    typed: dict[str, Any] = {
        "timezone": config["timezone"],
        "persona": notif["persona"],
        "notify_address": notif["notify_address"],
        "quiet_start_hour": notif["quiet_start_hour"],
        "quiet_end_hour": notif["quiet_end_hour"],
        "daily_normal_cap": notif["daily_normal_cap"],
        "cooldown_minutes": notif["cooldown_minutes"],
        "feeds_enabled": config["sources"]["public_feeds"],
        "obligations_enabled": config["sources"]["obligations"],
        "mail_enabled": config["sources"]["mail"],
        "calendar_enabled": config["sources"]["calendar"],
        "tasks_enabled": config["sources"]["tasks"],
        "mail_accounts": list(config["mail"]["accounts"]),
        "mail_account_aliases": dict(config["mail"]["aliases"]),
        "calendar_accounts": list(config["calendar"]["accounts"]),
        "calendar_ids": list(config["calendar"]["calendars"]),
        "task_accounts": list(config["tasks"]["accounts"]),
        "task_lists": list(config["tasks"]["lists"]),
        "task_adapter": config["tasks"]["adapter"],
        "public_interests": list(config["public_interests"]),
        "adaptive_mode": config["adaptive"]["mode"],
        "adaptive_privacy_review_enabled": config["adaptive"]["privacy_review"],
        "adaptive_inferred_goals_enabled": config["adaptive"]["inferred_goals"],
        "collector_budget_seconds": config["bounds"]["collector_budget_seconds"],
        "max_candidates_per_kind": config["bounds"]["max_candidates_per_kind"],
        "feed_max_sources_per_run": config["bounds"]["feed_max_sources_per_run"],
        "productivity_secretary_enabled": config["adapters"]["productivity_secretary"],
        "gws_calendar_enabled": config["adapters"]["gws_calendar"],
        "_user_config_version": config.get("version", CONFIG_VERSION),
    }
    overrides.update(typed)
    # Explicit legacy migration may retain library alias discovery where the
    # original policy omitted account scope. Neutral defaults never set this.
    for kind in advanced.get("legacy_account_discovery", []):
        if not config[kind]["accounts"]:
            overrides.pop(f"{kind if kind == 'calendar' else 'task'}_accounts", None)
    if notif["destination"]:
        overrides["deliver_target"] = notif["destination"]
    if config["public_sources"]:
        overrides["feed_seed_sources"] = copy.deepcopy(config["public_sources"])
    return overrides


def apply_user_config(policy: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Return ``policy`` with the explicit user-configuration overrides applied."""
    merged = dict(policy)
    merged.update(policy_overrides(config))
    return merged


# --------------------------------------------------------------------------- #
# Native settings preference
# --------------------------------------------------------------------------- #

_UNSET = object()


def _classify_native(value: Any) -> tuple[str, Any]:
    """Classify a native ``user_config`` value as unset / ok / invalid.

    A present-but-malformed value is INVALID, never silently treated as absent:
    an object (or a JSON object string) is acceptable, an empty/absent value is
    unset, and anything else fails closed so a broken native setting can never be
    bypassed by a local file or built-in default.
    """
    if value is None or value is _UNSET:
        return "unset", None
    if isinstance(value, Mapping):
        return "ok", dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "unset", None
        try:
            parsed = json.loads(text)
        except ValueError:
            return "invalid", "native user_config setting is not valid JSON"
        if isinstance(parsed, dict):
            return "ok", parsed
        return "invalid", ("native user_config setting must be a JSON object, "
                           f"got {type(parsed).__name__}")
    return "invalid", (f"native user_config setting has unsupported type "
                       f"{type(value).__name__}")


def _read_native_setting(native: Any) -> tuple[str, Any]:
    """Read the native plugin settings ``user_config`` value, fail-closed.

    Accepts a real ``PluginContext``-like object exposing ``get_config`` (the
    supported accessor) or a plain mapping. Returns ``("unset"|"ok"|"invalid",
    value)``. A read FAILURE is ``invalid`` (not unset): a context that cannot be
    consulted must not silently degrade to a local file or built-in defaults.
    """
    if native is None:
        return "unset", None
    if isinstance(native, Mapping):
        if NATIVE_SETTINGS_KEY not in native:
            return "unset", None
        return _classify_native(native.get(NATIVE_SETTINGS_KEY))
    getter = getattr(native, "get_config", None)
    if not callable(getter):
        getter = native if callable(native) else None
    if getter is None:
        return "unset", None
    try:
        value = getter(NATIVE_SETTINGS_KEY, _UNSET)
    except Exception as exc:  # noqa: BLE001 - a failed read is fail-closed
        return "invalid", f"native user_config read failed ({type(exc).__name__})"
    return _classify_native(value)


@dataclass
class LoadedConfig:
    """Result of loading + validating a user configuration."""

    config: dict[str, Any]
    present: bool
    source: str = "defaults"
    warnings: list[str] = field(default_factory=list)
    migrated: bool = False
    migration_report: dict[str, Any] = field(default_factory=dict)

    @property
    def version(self) -> int:
        return int(self.config.get("version", CONFIG_VERSION))

    def summary(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "source": self.source,
            "version": self.version,
            "timezone": self.config["timezone"],
            "sources": dict(self.config["sources"]),
            "mail_accounts": list(self.config["mail"]["accounts"]),
            "calendars": list(self.config["calendar"]["calendars"]),
            "adaptive_mode": self.config["adaptive"]["mode"],
            "adapters": dict(self.config["adapters"]),
            "warnings": list(self.warnings),
        }


def load_user_config(
    path: str | Path | None = None,
    *,
    native: Any = None,
    migrate: bool = False,
    strict: bool = True,
) -> LoadedConfig:
    """Load, validate and (when asked) migrate a user configuration.

    Preference order: a native plugin ``user_config`` setting, then the explicit
    file at ``path``. A present-but-stale version is only accepted when
    ``migrate`` is true; an unknown/corrupt version always fails closed.
    """
    native_state, native_raw = _read_native_setting(native)
    source = "defaults"
    raw: Any = None
    if native_state == "invalid":
        raise ConfigError(
            f"invalid native user_config setting: {native_raw}",
            remedy=("fix plugins.entries."
                    f"{NATIVE_PLUGIN_ID}.settings.{NATIVE_SETTINGS_KEY} to a JSON "
                    "object or remove it (a malformed setting never falls back)"),
        )
    if native_state == "ok":
        raw, source = native_raw, "native"
    elif path is not None and Path(path).exists():
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"cannot read user configuration {path}: {exc}",
                remedy="fix or remove the file, or re-run `hermes secretary configure init`",
            ) from exc
        source = "file"

    if raw is None:
        config = default_config()
        return LoadedConfig(config=config, present=False, source="defaults")

    config = validate_config(raw)
    explicit_version = config.get("_explicit_version", CONFIG_VERSION)
    migrated = False
    report: dict[str, Any] = {}
    if explicit_version < CONFIG_VERSION:
        if not migrate:
            raise ConfigError(
                f"config version {explicit_version} requires migration to {CONFIG_VERSION}",
                remedy="run `hermes secretary configure migrate --from-policy <policy.json>` "
                       "or `hermes secretary configure migrate --file <config.json>`",
            )
        config, report = migrate_config(config)
        migrated = True
    if not strict:
        config.pop("_warnings", None)
    warnings = list(config.get("_warnings", []))
    return LoadedConfig(config=config, present=True, source=source,
                        warnings=warnings, migrated=migrated, migration_report=report)


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #

def migrate_legacy(
    legacy_policy: dict[str, Any] | None,
    *,
    legacy_preferences: dict[str, Any] | None = None,
    destination: str = "",
    mail_accounts: list[str] | None = None,
    calendar_accounts: list[str] | None = None,
    calendar_ids: list[str] | None = None,
    task_accounts: list[str] | None = None,
    task_lists: list[str] | None = None,
    task_adapter: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map a legacy policy/preferences tree onto an explicit v1 configuration.

    The mapping preserves the legacy behaviour rather than widening it. Explicit
    migration inputs (``mail_accounts``/``calendar_accounts``/``calendar_ids``/
    ``task_accounts``/``task_lists``/``task_adapter``) win over retained policy
    fields, including explicit empty scopes. Formerly omitted calendar/task
    accounts retain library discovery through an explicit migration marker;
    mail retains the original formal/informal fallback only when omitted.
    EVERY remaining effective legacy policy field is retained as an explicit,
    reported compatibility passthrough so no setting is silently dropped. Global
    pause in the legacy preferences is deliberately NOT copied (it stays in
    preferences, so migration can never silently unpause or pause).
    """
    policy = legacy_policy if isinstance(legacy_policy, dict) else {}
    config = default_config()
    report: dict[str, Any] = {"from": "legacy-policy", "mapped": [], "notes": [],
                              "retained_effective_fields": []}

    timezone = policy.get("timezone")
    if isinstance(timezone, str) and _SAFE_TIMEZONE.fullmatch(timezone.strip()):
        config["timezone"] = timezone.strip()
        report["mapped"].append("timezone")
    else:
        report["notes"].append(f"legacy timezone {timezone!r} not carried; neutral UTC used")

    notif = config["notification"]
    notif["persona"] = str(policy.get("persona") or notif["persona"])[:64]
    notif["notify_address"] = str(policy.get("notify_address") or notif["notify_address"])[:64]
    notif["destination"] = destination.strip() if isinstance(destination, str) else ""
    for src_key, dst_key in (("quiet_start_hour", "quiet_start_hour"),
                             ("quiet_end_hour", "quiet_end_hour"),
                             ("daily_normal_cap", "daily_normal_cap"),
                             ("cooldown_minutes", "cooldown_minutes")):
        if _is_int(policy.get(src_key)):
            notif[dst_key] = policy[src_key]
    report["mapped"].append("notification")

    config["sources"]["public_feeds"] = bool(policy.get("feeds_enabled", True))
    config["sources"]["obligations"] = bool(policy.get("obligations_enabled", True))
    # The legacy private connectors were always attempted; make that explicit.
    for kind in ("mail", "calendar", "tasks"):
        config["sources"][kind] = _bounded_bool(
            policy.get(f"{kind}_enabled"), f"{kind}_enabled", True)
    if mail_accounts is not None:
        config["mail"]["accounts"] = _validate_str_list(mail_accounts, "mail.accounts", _SAFE_ACCOUNT)
        report["mapped"].append("mail.accounts (explicit migration input)")
    else:
        config["mail"]["accounts"] = _validate_str_list(
            policy.get("mail_accounts", ["formal", "informal"]), "mail.accounts", _SAFE_ACCOUNT)
        report["mapped"].append("mail.accounts (prior policy or legacy formal/informal fallback)")
    config["mail"]["aliases"] = copy.deepcopy(policy.get("mail_account_aliases", {}))
    discovery: list[str] = []
    if calendar_accounts is None and "calendar_accounts" in policy:
        calendar_accounts = policy["calendar_accounts"]
    if calendar_accounts is not None:
        config["calendar"]["accounts"] = _validate_str_list(
            calendar_accounts, "calendar.accounts", _SAFE_ACCOUNT)
        report["mapped"].append("calendar.accounts (explicit migration input)")
    elif "calendar_accounts" not in policy:
        discovery.append("calendar")
    if calendar_ids is None and "calendar_ids" in policy:
        calendar_ids = policy["calendar_ids"]
    if calendar_ids is not None:
        config["calendar"]["calendars"] = _validate_str_list(
            calendar_ids, "calendar.calendars", _SAFE_CALENDAR)
        report["mapped"].append("calendar.calendars (explicit migration input)")
    if task_accounts is None:
        if "task_accounts" in policy:
            task_accounts = policy["task_accounts"]
        elif calendar_accounts is not None:
            task_accounts = calendar_accounts
            report["notes"].append("legacy shared calendar aliases copied to tasks.accounts")
        else:
            discovery.append("tasks")
    if task_accounts is not None:
        config["tasks"]["accounts"] = _validate_str_list(
            task_accounts, "tasks.accounts", _SAFE_ACCOUNT)
        report["mapped"].append("tasks.accounts (explicit/retained legacy scope)")
    if task_lists is None and "task_lists" in policy:
        task_lists = policy["task_lists"]
    if task_lists is not None:
        config["tasks"]["lists"] = _validate_str_list(task_lists, "tasks.lists", _SAFE_LABEL)
        report["mapped"].append("tasks.lists (explicit migration input)")
    if task_adapter is None and "task_adapter" in policy:
        task_adapter = policy["task_adapter"]
    if task_adapter is not None:
        adapter_text = str(task_adapter).strip()
        if adapter_text and not _SAFE_ACCOUNT.fullmatch(adapter_text):
            raise ConfigError("task_adapter is not a safe adapter name")
        config["tasks"]["adapter"] = adapter_text
        report["mapped"].append("tasks.adapter (explicit migration input)")
    config["public_interests"] = _validate_str_list(
        policy.get("public_interests"), "public_interests", _SAFE_LABEL)

    seeds = policy.get("feed_seed_sources")
    if isinstance(seeds, list):
        try:
            config["public_sources"] = _validate_public_sources(seeds)
            report["mapped"].append("public_sources")
        except ConfigError as exc:
            config["public_sources"] = []
            report["notes"].append(f"legacy feed_seed_sources not carried: {exc}")

    mode = policy.get("adaptive_mode")
    if mode in {"disabled", "shadow", "active"}:
        config["adaptive"]["mode"] = mode
    config["adaptive"]["privacy_review"] = bool(policy.get("adaptive_privacy_review_enabled", False))
    config["adaptive"]["inferred_goals"] = bool(policy.get("adaptive_inferred_goals_enabled", False))
    report["mapped"].append("adaptive")

    config["bounds"]["collector_budget_seconds"] = _bounded_int(
        policy.get("collector_budget_seconds"), "collector_budget_seconds", 5, 600, 60)
    config["bounds"]["max_candidates_per_kind"] = _bounded_int(
        policy.get("max_candidates_per_kind"), "max_candidates_per_kind", 1, 500, 60)
    config["bounds"]["feed_max_sources_per_run"] = _bounded_int(
        policy.get("feed_max_sources_per_run"), "feed_max_sources_per_run", 0, 20, 2)
    report["mapped"].append("bounds")

    # Preserve the compatibility adapter and the shipped secretary library route.
    config["adapters"]["productivity_secretary"] = _bounded_bool(
        policy.get("productivity_secretary_enabled"), "productivity_secretary_enabled", True)
    config["adapters"]["gws_calendar"] = _bounded_bool(
        policy.get("gws_calendar_enabled"), "gws_calendar_enabled", False)
    report["mapped"].append("adapters.productivity_secretary")

    # Complete effective parity: retain every remaining legacy policy key as an
    # explicit, reported compatibility field. The typed mapping above takes
    # precedence over these when the policy is rebuilt, so nothing is widened and
    # nothing is silently dropped.
    _mapped_keys = {
        "timezone", "persona", "notify_address", "quiet_start_hour", "quiet_end_hour",
        "daily_normal_cap", "cooldown_minutes", "feeds_enabled", "obligations_enabled",
        "feed_seed_sources", "adaptive_mode", "adaptive_privacy_review_enabled",
        "adaptive_inferred_goals_enabled", "collector_budget_seconds",
        "max_candidates_per_kind", "feed_max_sources_per_run",
    }
    retained = {k: copy.deepcopy(policy[k]) for k in policy if k not in _mapped_keys}
    config["advanced"] = {"legacy_policy": retained}
    if discovery:
        config["advanced"]["legacy_account_discovery"] = discovery
    report["source_mappings"] = {
        "mail": copy.deepcopy(config["mail"]),
        "calendar": copy.deepcopy(config["calendar"]),
        "tasks": copy.deepcopy(config["tasks"]),
        "adapters": copy.deepcopy(config["adapters"]),
        "legacy_account_discovery": discovery,
    }
    report["notes"].extend([
        "empty calendar IDs retain selected/primary/holiday calendar reads within configured accounts",
        "empty task lists retain all-visible-list reads within configured task accounts",
        "omitted legacy account scopes retain library discovery only through explicit migration",
    ])
    report["retained_effective_fields"] = sorted(retained)
    if retained:
        report["mapped"].append("advanced.legacy_policy (complete effective parity)")

    if legacy_preferences is not None:
        report["notes"].append(
            "preferences (explicit/inferred/snoozes/pause) were not moved: they stay "
            "in preferences.json so migration cannot silently pause or unpause")

    config["version"] = CONFIG_VERSION
    return validate_config(config), report


def migrate_config(stale: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the only in-place migration path: a stale version is revalidated.

    This never invents fields; it normalises the existing mapping to the current
    schema after explicit opt-in. Unknown future versions are rejected earlier.
    """
    normalized = copy.deepcopy(stale)
    normalized["version"] = CONFIG_VERSION
    normalized["schema"] = CONFIG_SCHEMA
    migrated = validate_config(normalized)
    return migrated, {"from": f"version {stale.get('version')}", "to": CONFIG_VERSION,
                      "mapped": ["normalised to current schema"]}


def schema_summary() -> dict[str, Any]:
    """Machine-readable description of the configuration surface for docs/CLI."""
    return {
        "schema": CONFIG_SCHEMA,
        "version": CONFIG_VERSION,
        "timezone_default": DEFAULT_TIMEZONE,
        "sections": list(_SECTIONS),
        "credential_values_allowed": False,
        "native_settings": f"plugins.entries.{NATIVE_PLUGIN_ID}.settings.{NATIVE_SETTINGS_KEY}",
    }


__all__ = [
    "CONFIG_SCHEMA", "CONFIG_VERSION", "DEFAULT_TIMEZONE", "NATIVE_SETTINGS_KEY",
    "ConfigError", "LoadedConfig", "default_config", "validate_config",
    "load_user_config", "migrate_legacy", "migrate_config", "policy_overrides",
    "apply_user_config", "schema_summary",
]
