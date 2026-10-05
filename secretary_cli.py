"""``hermes secretary`` CLI for the hermes-proactive-secretary plugin.

This module is imported lazily (only when the ``secretary`` command tree is
built or run) so unrelated ``hermes`` startup is unaffected. It never touches a
source, the network, or the scheduler at import time.

Commands
    setup      one-command, idempotent, verified setup
    status     enabled/disabled, job state, last run, coverage, counts
    pause      global pause (stops collection AND delivery)
    resume     clear the global pause
    disable    pause + remove this plugin's own cron jobs
    uninstall  alias of disable (data is preserved, never deleted)
    self-test  offline deterministic self-test
    run-once   dry, no-model run that never sends
    watch      add | list | remove a public watch
    feedback   passthrough to the validated feedback command
    doctor     environment and wiring diagnostics

Every command supports ``--json`` and returns a non-zero exit on failure.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import cron as cron_mod
from . import paths
from . import runner_install
from . import state_import

# Host-built native context captured from the registered CLI handler closure.
# It is only ever the context Hermes constructs and passes to ``register(ctx)``;
# this module never builds one. Model calls through it stay host-owned and
# gated. ``None`` until the host wires the plugin.
_NATIVE_CONTEXT: Any = None


def set_native_context(ctx: Any) -> None:
    """Record the host-provided context from the plugin registration closure."""
    global _NATIVE_CONTEXT
    _NATIVE_CONTEXT = ctx


def get_native_context() -> Any:
    """Return the host-provided context (or ``None`` in an offline/test process)."""
    return _NATIVE_CONTEXT


def _ensure_src() -> None:
    src = str(paths.plugin_root() / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def _json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")


def _common_parent() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir", default=None,
        help="override the owner-only runtime data directory (advanced/testing)",
    )
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")
    return common


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes secretary`` argparse tree."""
    common = _common_parent()
    subs = subparser.add_subparsers(dest="secretary_command")
    p = {name: subs.add_parser(name, parents=[common], help=help_)
         for name, help_ in (
             ("setup", "One-command setup: policy, state, native cron jobs, verification"),
             ("status", "Show enabled state, jobs, last run, coverage and counts"),
             ("pause", "Global pause: stop collection and delivery"),
             ("resume", "Clear the global pause"),
             ("disable", "Pause and remove this plugin's own cron jobs"),
             ("uninstall", "Alias of disable (runtime data is preserved)"),
             ("self-test", "Run the offline deterministic self-test"),
             ("run-once", "Dry, no-model run (never sends)"),
             ("watch", "Manage public watches (add | list | remove)"),
             ("feedback", "Record or inspect notification preferences"),
             ("configure", "Validated per-user configuration (show | validate | preview | init | migrate)"),
             ("pipeline", "Internal: scheduled ordinary pipeline via the native context bridge"),
             ("reminders", "Requested reminders (dispatch | check | list | status | add | cancel)"),
             ("doctor", "Check the environment and wiring"),
          )}
    p["setup"].add_argument("--force", action="store_true",
                            help="overwrite an existing policy file (default: keep it)")
    p["setup"].add_argument("--no-cron", action="store_true",
                            help="skip native cron job creation (offline verification only)")
    p["setup"].add_argument("--import-from", default=None, metavar="LEGACY_TREE",
                            help="copy durable legacy state (var/, policy.json, preferences.json) "
                                 "before arming anything; refuses conflicting targets")
    p["setup"].add_argument("--import-dry-run", action="store_true",
                            help="preview --import-from only: inventory and hashes, no changes")
    p["setup"].add_argument("--fixture", default="healthy_empty",
                            help="offline fixture used for the no-model dry-run")
    p["setup"].add_argument("--deliver", default=None,
                            help="delivery target for alerts (e.g. local, origin, platform:chat_id). "
                                 "Defaults to 'local' with a warning; never guessed.")
    p["run-once"].add_argument("--fixture", default=None,
                               help="run against a synthetic fixture instead of live sources")
    p["run-once"].add_argument("--check", action="store_true",
                               help="collect-only coverage check (implies dry, never sends)")
    p["run-once"].add_argument("--model", action="store_true",
                               help="use the host ctx.llm lane (default: deterministic no-model)")
    p["pipeline"].add_argument("--fixture", default=None,
                               help="run against a synthetic fixture instead of live sources")
    p["pipeline"].add_argument("--check", action="store_true",
                               help="collect-only coverage check (never sends)")
    p["reminders"].add_argument("reminder_args", nargs=argparse.REMAINDER,
                                help="arguments forwarded to the validated reminder CLI "
                                     "(default: dispatch)")
    for _name in ("disable", "uninstall"):
        p[_name].add_argument("--purge", action="store_true",
                              help="also delete the owner-only runtime data directory")
    p["watch"].add_argument("action", choices=("add", "list", "remove"))
    p["watch"].add_argument("value", nargs="?", default="",
                            help="topic for add, watch id for remove")
    p["watch"].add_argument("--scope", default="public")
    p["watch"].add_argument("--cadence-minutes", type=int, default=None)
    p["feedback"].add_argument("feedback_args", nargs=argparse.REMAINDER,
                               help="arguments forwarded to the validated feedback CLI")
    p["configure"].add_argument("action",
                                choices=("show", "validate", "preview", "init", "migrate"),
                                help="show/validate/preview the config, init a neutral "
                                     "example, or migrate a legacy policy")
    p["configure"].add_argument("--file", default=None,
                                help="user configuration path (default: <data>/user_config.json)")
    p["configure"].add_argument("--from-policy", default=None, metavar="POLICY",
                                help="legacy policy.json to migrate from")
    p["configure"].add_argument("--mail-account", action="append", default=None,
                                metavar="NAME",
                                help="migrate: copy an existing configured mail account (repeatable)")
    p["configure"].add_argument("--calendar-account", action="append", default=None,
                                metavar="NAME",
                                help="migrate: copy an existing configured calendar account (repeatable)")
    p["configure"].add_argument("--calendar-id", action="append", default=None,
                                metavar="ID",
                                help="migrate: copy an existing configured calendar id (repeatable)")
    p["configure"].add_argument("--task-list", action="append", default=None,
                                metavar="NAME",
                                help="migrate: copy an existing configured task list (repeatable)")
    p["configure"].add_argument("--task-account", action="append", default=None,
                                metavar="NAME",
                                help="migrate: copy an existing configured task account (repeatable)")
    p["configure"].add_argument("--adapter", default=None,
                                help="migrate: preserve the named compatibility adapter selection")
    p["configure"].add_argument("--force", action="store_true",
                                help="overwrite an existing nonempty configuration")
    p["configure"].add_argument("--dry-run", action="store_true",
                                help="preview only; write nothing")
    subparser.set_defaults(func=secretary_command)


def _data_override(args: argparse.Namespace) -> None:
    override = getattr(args, "data_dir", None)
    if override:
        os.environ[paths.DATA_ENV] = str(override)


def _emit(obj: Any, *, as_json: bool, human: str) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))
    else:
        print(human)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _prefs() -> dict[str, Any]:
    _ensure_src()
    from proactive_pilot.feedback import load_preferences_file
    return load_preferences_file(paths.preferences_path(), strict=False)


def _is_paused() -> bool:
    paused = _prefs().get("paused") or {}
    return bool(isinstance(paused, dict) and paused.get("active"))


def _backend() -> cron_mod.HermesCronBackend:
    return cron_mod.HermesCronBackend()


# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #

OWNED_JOB_NAMES = cron_mod.OWNED_JOB_NAMES


def _write_policy(force: bool) -> dict[str, Any]:
    default = paths.default_policy_path()
    if not default.is_file():
        raise FileNotFoundError(f"shipped default policy missing: {default}")
    target = paths.policy_path()
    if target.exists() and not force:
        return {"policy": str(target), "action": "kept"}
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(default.read_text(encoding="utf-8"))
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    return {"policy": str(target), "action": "written"}


def _ensure_user_config() -> dict[str, Any]:
    """Prepare the neutral per-user configuration for a NEW generic install.

    Idempotent and never overwriting: if a configuration already exists it is
    kept. Only the shipped neutral example is written, so a fresh install starts
    with generic defaults (no accounts, no seeds, adaptive off) instead of any
    implicit private-account assumption.
    """
    target = paths.user_config_path()
    if target.exists() and target.read_text(encoding="utf-8").strip():
        return {"user_config": str(target), "action": "kept"}
    example = paths.user_config_example_path()
    if not example.is_file():
        return {"user_config": str(target), "action": "missing-example"}
    payload = json.loads(example.read_text(encoding="utf-8"))
    status = _write_user_config(target, payload, force=False)
    return {"user_config": str(target), "action": status}


def _ensure_preferences() -> dict[str, Any]:
    _ensure_src()
    from proactive_pilot.clock import Clock
    from proactive_pilot.feedback import empty_preferences, load_preferences_file, save_preferences_file
    target = paths.preferences_path()
    if target.exists():
        return {"preferences": str(target), "action": "kept"}
    prefs = empty_preferences()
    save_preferences_file(target, prefs, Clock().now(), lock=False)
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    return {"preferences": str(target), "action": "written"}


def detect_capabilities() -> dict[str, Any]:
    """Best-effort, read-only connector detection (never installs anything)."""
    _ensure_src()
    himalaya = shutil.which("himalaya") is not None
    secretary_src = Path(_secretary_src())
    secretary = bool(str(secretary_src)) and secretary_src.is_dir()
    monitoring = "full" if (secretary or himalaya) else "public-only"
    return {
        "himalaya_mail": himalaya,
        "productivity_secretary": secretary,
        "secretary_src": str(secretary_src),
        "secretary_src_candidates": _secretary_src_candidates(),
        "secretary_src_env": "SECRETARY_SRC",
        "calendar_tasks": secretary,
        "public_sources": True,
        "monitoring": monitoring,
    }


def _secretary_src() -> str:
    try:
        from proactive_pilot.collectors import resolve_secretary_src
        return resolve_secretary_src()
    except Exception:  # noqa: BLE001 - detection must never fail setup
        return str(paths.hermes_home() / "productivity-secretary" / "src")


def _secretary_src_candidates() -> list[str]:
    try:
        from proactive_pilot.collectors import secretary_src_candidates
        return secretary_src_candidates()
    except Exception:  # noqa: BLE001 - detection must never fail setup
        return [str(paths.hermes_home() / "productivity-secretary" / "src"),
                str(Path.home() / "productivity-secretary" / "src")]


def _secretary_env_hint() -> str:
    return "SECRETARY_SRC (or PRODUCTIVITY_SECRETARY_SRC)"


def _authoritative_config() -> Any:
    """Load the authoritative validated configuration (native settings first).

    Raises ``userconfig.ConfigError`` when a present native/file configuration is
    invalid. A completely absent configuration is NOT an error (neutral defaults).
    """
    _ensure_src()
    from proactive_pilot import userconfig
    return userconfig.load_user_config(paths.user_config_path(), native=get_native_context())


def _run_offline_checks(fixture: str) -> dict[str, Any]:
    _ensure_src()
    from proactive_pilot.selftest import run_self_test
    from proactive_pilot.main import build_parser, run_once

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        test_rc = run_self_test()
    try:
        test_report = json.loads(buf.getvalue())
    except ValueError:
        test_report = {"raw": buf.getvalue()[-500:]}

    argv = [
        "--state-dir", str(paths.state_dir()),
        "--policy", str(paths.policy_path()),
        "--preferences", str(paths.preferences_path()),
        "--fixture", fixture, "--dry-run", "--no-model",
    ]
    ns = build_parser().parse_args(argv)
    # Thread the host-built context so the authoritative (native) configuration
    # is validated during the offline check too; a no-model fixture run reads no
    # private connector and no location context.
    ns.native_ctx = get_native_context()
    dry_rc = run_once(ns)
    return {
        "self_test_rc": test_rc,
        "self_test": {k: test_report.get(k) for k in ("total", "passed", "failed")},
        "dry_run_rc": dry_rc,
    }


def setup_command(args: argparse.Namespace, *, backend: cron_mod.CronBackend | None = None) -> int:
    """One-command idempotent setup. Returns 0 only on a fully verified state.

    Ordering is deliberate: data/policy/preferences are prepared (migration
    first if requested), the offline self-test + no-send fixture check run, and
    only THEN are the verified runners installed and the cron jobs armed. A
    failing check therefore creates zero cron jobs.
    """
    as_json = bool(getattr(args, "json", False))
    import_from = getattr(args, "import_from", None)
    if bool(getattr(args, "import_dry_run", False)):
        if not import_from:
            _emit({"ok": False, "error": "--import-dry-run requires --import-from <legacy_tree>"},
                  as_json=as_json,
                  human="setup --import-dry-run requires --import-from <legacy_tree>")
            return 2
        return _import_preview(import_from, as_json)

    result: dict[str, Any] = {
        "plugin": paths.PLUGIN_NAME,
        "data_root": str(paths.data_root()),
        "steps": [],
        "jobs": [],
        "runners": [],
        "warnings": [],
    }
    deliver_arg = getattr(args, "deliver", None)
    created: list[str] = []
    try:
        paths.ensure_data_dirs()
        result["steps"].append({"step": "data-dir", "status": "ok",
                                "detail": str(paths.data_root())})
        if import_from:
            imported = state_import.import_legacy(str(import_from))
            result["import"] = imported
            result["steps"].append({
                "step": "import", "status": "ok",
                "detail": f"{imported['counts']['copied']} copied, "
                          f"{imported['counts']['identical']} identical"})
        result["policy"] = _write_policy(bool(getattr(args, "force", False)))
        result["preferences"] = _ensure_preferences()
        # Prepare the generic configuration on a NEW install BEFORE any runner or
        # cron work. An existing deployment keeps its policy and must migrate its
        # own configuration explicitly; a neutral config is never forced over it.
        if result["policy"].get("action") == "written" and not import_from:
            result["user_config"] = _ensure_user_config()
        else:
            _uc = paths.user_config_path()
            result["user_config"] = {
                "user_config": str(_uc),
                "action": "kept" if (_uc.exists() and _uc.read_text(
                    encoding="utf-8").strip()) else
                    "deferred (existing policy; run `hermes secretary configure migrate`)",
            }
        # Validate the AUTHORITATIVE configuration with the host context BEFORE
        # any runner or cron work. A present-but-invalid native/file setting is a
        # hard failure: it can never be ignored by a fixture or a file default.
        loaded_cfg = _authoritative_config()
        result["user_config_effective"] = {
            "present": bool(loaded_cfg.present),
            "source": loaded_cfg.source,
            "destination": loaded_cfg.config["notification"]["destination"],
        }
        # Effective notification destination: explicit --deliver wins, else a
        # real configured destination. 'local' is the safe fallback (record but
        # do not deliver), so it is not treated as a configured target; only when
        # NEITHER is set do we warn.
        configured_dest = (loaded_cfg.config["notification"]["destination"]
                           if loaded_cfg.present else "").strip()
        effective_dest = "" if configured_dest.lower() == "local" else configured_dest
        if deliver_arg:
            deliver = str(deliver_arg).strip()
        elif effective_dest:
            deliver = effective_dest
        else:
            deliver = "local"
        result["capabilities"] = detect_capabilities()

        # Verify BEFORE installing runners or arming cron.
        result["checks"] = _run_offline_checks(str(getattr(args, "fixture", "healthy_empty")))
        checks_ok = (result["checks"].get("self_test_rc") == 0
                     and result["checks"].get("dry_run_rc") == 0)
        result["steps"].append({
            "step": "verify", "status": "ok" if checks_ok else "failed",
            "detail": f"self-test rc={result['checks'].get('self_test_rc')}, "
                      f"dry-run rc={result['checks'].get('dry_run_rc')}"})
        if not checks_ok:
            raise RuntimeError(
                "offline verification failed "
                f"(self-test rc={result['checks'].get('self_test_rc')}, "
                f"dry-run rc={result['checks'].get('dry_run_rc')})")

        # Verified: install the owned runner templates before creating jobs.
        result["runners"] = runner_install.install_runners(
            plugin_root=paths.plugin_root(),
            template_root=paths.runners_source_dir(),
            scripts_dir=paths.scripts_dir(),
            ownership_path=paths.runner_ownership_path(),
        )
        result["deliver"] = deliver
        if not deliver_arg and not effective_dest:
            result["warnings"].append(
                "no --deliver target given; defaulting to 'local'. Alerts will be recorded but "
                "not delivered until a target is set "
                "(e.g. --deliver origin or --deliver platform:chat_id)."
            )

        if not getattr(args, "no_cron", False):
            cron_backend = backend or _backend()
            if not cron_backend.is_available():
                raise cron_mod.CronError(
                    "the Hermes cron surface is unavailable; cannot create the two native jobs. "
                    "Re-run with a working `hermes cron` (or `--no-cron` for an offline check)."
                )
            specs = _job_specs(deliver)
            ownership = cron_mod.load_job_ownership(paths.job_ownership_path())
            for spec in specs:
                res = cron_mod.ensure_job(
                    cron_backend, spec,
                    ownership=ownership, ownership_path=paths.job_ownership_path(),
                )
                result["jobs"].append({**res.to_dict(), "schedule": spec.schedule,
                                       "deliver": spec.deliver, "script": spec.script})
                if res.action == "created":
                    created.append(spec.name)
        else:
            result["warnings"].append("cron jobs were skipped (--no-cron)")

        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - fail loudly and roll back honestly
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        if created and not getattr(args, "no_cron", False):
            rolled, failures = _rollback_created_jobs(created, backend)
            if rolled:
                result["rolled_back"] = rolled
            if failures:
                result["rollback_failures"] = failures
        _emit(result, as_json=as_json, human=_setup_human(result))
        return 1

    _emit(result, as_json=as_json, human=_setup_human(result))
    return 0


def _import_preview(legacy_tree: str, as_json: bool) -> int:
    """Preview an explicit legacy import: inventory/hashes only, no changes."""
    result: dict[str, Any] = {
        "plugin": paths.PLUGIN_NAME,
        "dry_run": True,
        "legacy_root": str(legacy_tree),
        "steps": [],
        "warnings": [],
    }
    try:
        manifest = state_import.preview(str(legacy_tree))
        result["import"] = manifest
        result["ok"] = bool(manifest.get("ok"))
    except Exception as exc:  # noqa: BLE001 - preview must fail loudly, not crash
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    _emit(result, as_json=as_json, human=_setup_human(result))
    return 0 if result["ok"] else 1


def _rollback_created_jobs(created: list[str],
                           backend: cron_mod.CronBackend | None) -> tuple[list[str], list[dict[str, Any]]]:
    """Remove only the jobs created in this run; report anything that stayed.

    A removal that raised or that did not actually remove the job is recorded as
    a failure rather than claimed as rolled back.
    """
    rolled: list[str] = []
    failures: list[dict[str, Any]] = []
    rollback_backend = backend or _backend()
    ownership = cron_mod.load_job_ownership(paths.job_ownership_path())
    for name in created:
        try:
            res = cron_mod.remove_job(
                rollback_backend, name,
                ownership=ownership, ownership_path=paths.job_ownership_path(),
            )
        except Exception as exc:  # noqa: BLE001 - honest failure, never suppressed
            failures.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if res.action in ("removed", "missing"):
            rolled.append(name)
        else:
            failures.append({"name": name, "action": res.action, "detail": res.detail})
    return rolled, failures


def _job_specs(deliver: str = "local") -> list[cron_mod.CronJobSpec]:
    return cron_mod.build_job_specs(deliver=deliver, workdir=str(paths.plugin_root()))


def _setup_human(result: dict[str, Any]) -> str:
    if result.get("dry_run") and "import" in result:
        imp = result["import"]
        counts = imp.get("counts", {})
        lines = [
            "Proactive Secretary legacy import preview (no changes made).",
            f"  legacy        : {imp.get('legacy_root', '?')}",
            f"  files         : {counts.get('total', 0)} total, "
            f"{counts.get('copied', 0)} to copy, {counts.get('identical', 0)} identical, "
            f"{counts.get('excluded', 0)} excluded, {counts.get('conflicts', 0)} conflicts",
            f"  manifest sha  : {imp.get('manifest_sha256', '')}",
        ]
        if imp.get("excluded"):
            lines.append(f"  excluded names: {', '.join(imp['excluded'])}")
        for conflict in imp.get("conflicts", []):
            lines.append(f"  conflict      : {conflict['rel']} -> {conflict['target']} "
                         f"({conflict.get('reason', '')})")
        if not result.get("ok"):
            lines.append(f"  error         : {result.get('error', 'conflicting target state')}")
        return "\n".join(lines)
    if not result.get("ok"):
        lines = ["Proactive Secretary setup FAILED.", f"  reason: {result.get('error', 'unknown')}"]
        if result.get("rolled_back"):
            lines.append(f"  rolled back jobs: {', '.join(result['rolled_back'])}")
        if result.get("rollback_failures"):
            lines.append(f"  rollback FAILURES (still present): "
                         f"{json.dumps(result['rollback_failures'], sort_keys=True)}")
        return "\n".join(lines)
    caps = result.get("capabilities", {})
    jobs = {j["name"]: j for j in result.get("jobs", [])}
    if caps.get("calendar_tasks") or caps.get("himalaya_mail"):
        monitored = ["public sources"]
        if caps.get("calendar_tasks"):
            monitored.append("calendar + tasks (shipped secretary library)")
        if caps.get("himalaya_mail"):
            monitored.append("authorised mail (himalaya, read-only metadata)")
    else:
        monitored = ["public sources only (no mail/calendar connector detected)"]
    lines = [
        "Proactive Secretary is set up and verified.",
        f"  data           : {result.get('data_root')}",
        f"  policy         : {result.get('policy', {}).get('action', '?')} "
        f"({result.get('policy', {}).get('policy', '')})",
        f"  monitored      : {', '.join(monitored)}",
        f"  deliver target : {result.get('deliver', 'local')}",
        "  NOT covered    : undated tasks; blocked/deferred/truncated sources; "
        "delivery confirmation is owned by the native scheduler",
        "  jobs           :",
    ]
    for name in OWNED_JOB_NAMES:
        job = jobs.get(name)
        lines.append(f"    - {name}: {job['action'] if job else 'MISSING'} "
                     f"[{job['schedule'] if job else '?'}]")
    if result.get("import"):
        counts = result["import"].get("counts", {})
        lines.append(f"  imported       : {counts.get('copied', 0)} copied, "
                     f"{counts.get('identical', 0)} identical, "
                     f"manifest {result['import'].get('manifest_sha256', '')[:12]}")
    checks = result.get("checks", {})
    lines.append(f"  self-test      : {checks.get('self_test', {})}")
    lines.append("  pause anytime  : hermes secretary pause")
    lines.append("  remove jobs    : hermes secretary disable")
    for warning in result.get("warnings", []):
        lines.append(f"  warning        : {warning}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# status / doctor
# --------------------------------------------------------------------------- #

def _job_states() -> dict[str, Any]:
    empty = {"available": False, "state": "unavailable", "jobs": [],
             "owned": [], "foreign": [], "missing": [], "error": ""}
    try:
        backend = _backend()
        if not backend.is_available():
            return dict(empty)
        jobs = backend.list_jobs()
    except Exception as exc:  # noqa: BLE001 - status must degrade, not crash
        result = dict(empty)
        result["error"] = str(exc)
        return result
    ownership = cron_mod.load_job_ownership(paths.job_ownership_path())
    classified = cron_mod.classify_jobs(jobs, ownership)
    classified["available"] = True
    return classified


def _jobs_human(jobs: dict[str, Any]) -> str:
    if not jobs.get("available"):
        return "unavailable"
    owned = jobs.get("owned", [])
    foreign = jobs.get("foreign", [])
    if not owned and not foreign:
        return "none (no jobs of ours)"
    text = f"{len(owned)} owned"
    if owned:
        text += f" [{', '.join(j.get('name', '?') for j in owned)}]"
    if foreign:
        text += (f"; WARNING {len(foreign)} foreign job(s) share our name and are "
                 f"left untouched [{', '.join(j.get('name', '?') for j in foreign)}]")
    missing = jobs.get("missing", [])
    if missing:
        text += f"; missing {', '.join(missing)}"
    return text


def status_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    state = _read_json(paths.state_dir() / "notify_state.json")
    reminders = _read_json(paths.state_dir() / "reminders.json")
    prefs = _prefs()
    caps = detect_capabilities()
    # Same authoritative native-first configuration/settings as setup/doctor and
    # the runtime pipeline (a present-but-invalid setting is reported, never
    # silently replaced by a file/default).
    config_error = ""
    config_summary: dict[str, Any] = {}
    destination = ""
    try:
        _loaded = _authoritative_config()
        config_summary = _loaded.summary()
        destination = _loaded.config["notification"]["destination"]
    except Exception as exc:  # noqa: BLE001 - status must degrade, not crash
        config_error = str(exc)
    active_reminders = sum(
        1 for r in reminders.get("reminders", {}).values()
        if isinstance(r, dict) and r.get("status") in {"pending", "uncertain"}
    )
    payload = {
        "plugin": paths.PLUGIN_NAME,
        "enabled": not _is_paused(),
        "paused": _is_paused(),
        "policy_present": paths.policy_path().is_file(),
        "data_root": str(paths.data_root()),
        "jobs": _job_states(),
        "capabilities": caps,
        "monitoring": caps.get("monitoring", "public-only"),
        "config": config_summary,
        "config_error": config_error,
        "deliver": destination or "local (default)",
        "last_pilot_run": (state.get("last_inference") or {}).get("at", ""),
        "coverage": state.get("health", {}),
        "counts": {
            "active_reminders": active_reminders,
            "explicit_preferences": len(prefs.get("explicit", [])),
            "inferred_preferences": len(prefs.get("inferred", [])),
            "snoozes": len(prefs.get("snoozes", [])),
        },
    }
    if caps.get("monitoring") == "public-only":
        monitoring = (f"public-only (calendar/tasks unavailable; set {_secretary_env_hint()} "
                      "to the library 'src' directory)")
    else:
        monitoring = caps.get("monitoring", "full")
    human = "\n".join([
        f"Proactive Secretary: {'DISABLED (paused)' if payload['paused'] else 'enabled'}",
        f"  data root     : {payload['data_root']}",
        f"  policy        : {'present' if payload['policy_present'] else 'MISSING'}",
        f"  config        : {'invalid: ' + config_error if config_error else ('present' if config_summary.get('present') else 'defaults (neutral)')}",
        f"  deliver       : {payload['deliver']}",
        f"  monitoring    : {monitoring}",
        f"  cron backend  : {'available' if payload['jobs'].get('available') else 'unavailable'}",
        f"  jobs          : {_jobs_human(payload['jobs'])}",
        f"  last pilot run: {payload['last_pilot_run'] or 'never recorded'}",
        f"  active reminders: {active_reminders}",
    ])
    _emit(payload, as_json=as_json, human=human)
    return 0


def doctor_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, critical: bool = True) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "critical": critical})

    root = paths.plugin_root()
    add("plugin_root", root.is_dir(), str(root))
    add("src_package", (root / "src" / "proactive_pilot" / "__init__.py").is_file(),
        str(root / "src" / "proactive_pilot"))
    add("default_policy", paths.default_policy_path().is_file(), str(paths.default_policy_path()))
    templates = paths.runners_source_dir()
    for runner in cron_mod.PILOT_SCRIPT, cron_mod.REMINDER_SCRIPT:
        add(f"runner_template:{runner}", (templates / runner).is_file(), str(templates / runner))
    add("scripts_dir_writable", _writable(paths.scripts_dir()), str(paths.scripts_dir()))
    add("data_dir_writable", _writable(paths.data_root()), str(paths.data_root()))
    jobs = _job_states()
    add("cron_backend", bool(jobs.get("available")), json.dumps(jobs.get("error", "available") or "available"))
    caps = detect_capabilities()
    add("himalaya_mail", caps["himalaya_mail"],
        "read-only mail connector" if caps["himalaya_mail"]
        else "not found; install/authorise himalaya to enable mail monitoring",
        critical=False)
    if caps["productivity_secretary"]:
        add("productivity_secretary", True, caps["secretary_src"], critical=False)
    else:
        add("productivity_secretary", False,
            "not found (searched: "
            + ", ".join(caps.get("secretary_src_candidates", []))
            + f"); calendar/tasks unavailable, set {_secretary_env_hint()} to the "
              "library 'src' directory to enable them", critical=False)
    add("public_sources", True, "public-source monitoring works without mail/calendar/tasks",
        critical=False)
    add("monitoring_mode", True, caps.get("monitoring", "public-only"), critical=False)
    # Validated user configuration + optional adapters (never credential reads).
    # The AUTHORITATIVE native-first configuration is loaded with the host ctx;
    # a present-but-invalid setting is a CRITICAL failure and can never be
    # ignored via a fixture or a file default.
    gws_enabled = False
    try:
        _loaded = _authoritative_config()
        gws_enabled = bool(_loaded.config.get("adapters", {}).get("gws_calendar"))
        _destination = _loaded.config["notification"]["destination"]
        add("user_config_valid", True,
            ("present" if _loaded.present else "defaults (no user configuration)")
            + f"; deliver={_destination or 'local (default)'}",
            critical=False)
    except Exception as exc:  # noqa: BLE001 - invalid config is a hard doctor failure
        add("user_config_valid", False,
            f"invalid configuration: {exc} "
            "(run `hermes secretary configure validate` or `configure migrate`)",
            critical=True)
    try:
        from proactive_pilot.gws_calendar import readiness_report
        gws = readiness_report({"gws_calendar_enabled": gws_enabled})
        add("gws_calendar", bool(gws.get("ready")),
            f"{gws.get('reason')} (backend={gws.get('backend') or 'none'})",
            critical=False)
    except Exception as exc:  # noqa: BLE001
        add("gws_calendar", False, f"probe failed: {type(exc).__name__}", critical=False)
    add("native_context", get_native_context() is not None,
        "host ctx available" if get_native_context() is not None else "offline (subprocess route)",
        critical=False)
    failed = [c for c in checks if c["critical"] and not c["ok"]]
    payload = {"ok": not failed, "checks": checks, "failed": [c["check"] for c in failed],
               "capabilities": caps}
    human = "\n".join(
        f"[{'ok' if c['ok'] else ('FAIL' if c['critical'] else 'off')}] "
        f"{c['check']}: {c['detail']}" for c in checks
    )
    _emit(payload, as_json=as_json, human=human)
    return 0 if not failed else 1



def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return os.access(path, os.W_OK)
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# pause / resume / disable / uninstall
# --------------------------------------------------------------------------- #

def _apply_feedback(action: str, provenance: str) -> int:
    _ensure_src()
    from proactive_pilot.clock import Clock
    from proactive_pilot.feedback import (
        FeedbackError, apply_action, load_preferences_file, save_preferences_file,
    )
    now = Clock().now()
    path = paths.preferences_path()
    prefs = load_preferences_file(path, strict=True)
    updated = apply_action(prefs, {"action": action, "provenance": provenance}, now)
    save_preferences_file(path, updated, now)
    return 0


def pause_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    try:
        _apply_feedback("pause", "hermes secretary pause")
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "error": str(exc)}, as_json=as_json, human=f"pause failed: {exc}")
        return 1
    _emit({"ok": True, "paused": True}, as_json=as_json,
          human="Proactive Secretary paused: collection and delivery are stopped.")
    return 0


def resume_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    try:
        _apply_feedback("resume", "hermes secretary resume")
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "error": str(exc)}, as_json=as_json, human=f"resume failed: {exc}")
        return 1
    _emit({"ok": True, "paused": False}, as_json=as_json,
          human="Proactive Secretary resumed: monitoring is active.")
    return 0


def _remove_own_jobs() -> list[dict[str, Any]]:
    backend = _backend()
    removed: list[dict[str, Any]] = []
    if not backend.is_available():
        raise cron_mod.CronError("the Hermes cron surface is unavailable; cannot remove jobs")
    ownership = cron_mod.load_job_ownership(paths.job_ownership_path())
    for name in OWNED_JOB_NAMES:
        res = cron_mod.remove_job(
            backend, name,
            ownership=ownership, ownership_path=paths.job_ownership_path(),
        )
        removed.append(res.to_dict())
    return removed


def _remove_own_runners() -> list[dict[str, Any]]:
    return runner_install.uninstall_runners(
        ownership_path=paths.runner_ownership_path(),
        scripts_dir=paths.scripts_dir(),
    )


def disable_command(args: argparse.Namespace, *, uninstall: bool = False) -> int:
    as_json = bool(getattr(args, "json", False))
    result: dict[str, Any] = {"ok": False, "removed": [], "scripts": [], "paused": False}
    try:
        _apply_feedback("pause", "hermes secretary disable")
        result["paused"] = True
        result["removed"] = _remove_own_jobs()
        result["scripts"] = _remove_own_runners()
        if uninstall and getattr(args, "purge", False):
            result["data_purged"] = _purge_data()
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        _emit(result, as_json=as_json, human=f"{'uninstall' if uninstall else 'disable'} FAILED: {exc}")
        return 1
    removed_desc = ", ".join(f"{r['name']} ({r['action']})" for r in result["removed"])
    scripts_desc = ", ".join(
        f"{Path(r['file']).name} ({r['action']})" for r in result["scripts"]
    )
    human = (
        f"Proactive Secretary {'uninstalled' if uninstall else 'disabled'}.\n"
        f"  paused : yes (collection and delivery stopped)\n"
        f"  removed: {removed_desc or 'no owned jobs were present'}\n"
        f"  scripts: {scripts_desc or 'no owned runner scripts were present'}"
    )
    if uninstall:
        human += "\n  data   : runtime data preserved (pass --purge to delete it)"
    _emit(result, as_json=as_json, human=human)
    return 0


def _purge_data() -> bool:
    import shutil as _shutil
    root = paths.data_root()
    if root.exists():
        _shutil.rmtree(root, ignore_errors=True)
    return not root.exists()


def uninstall_command(args: argparse.Namespace) -> int:
    return disable_command(args, uninstall=True)


# --------------------------------------------------------------------------- #
# self-test / run-once / watch / feedback
# --------------------------------------------------------------------------- #

def selftest_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    _ensure_src()
    from proactive_pilot.selftest import run_self_test
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_self_test()
    try:
        report = json.loads(buf.getvalue())
    except ValueError:
        report = {"raw": buf.getvalue()[-1000:]}
    human = (f"self-test: {report.get('passed')}/{report.get('total')} passed "
             f"({report.get('failed')} failed)")
    _emit(report, as_json=as_json, human=human)
    return rc


def run_once_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    _ensure_src()
    from proactive_pilot.main import build_parser, run_once
    use_model = bool(getattr(args, "model", False))
    argv = [
        "--state-dir", str(paths.state_dir()),
        "--policy", str(paths.policy_path()),
        "--preferences", str(paths.preferences_path()),
    ]
    if not use_model:
        argv.append("--no-model")
    if getattr(args, "check", False):
        argv.append("--check")
    else:
        argv.append("--dry-run")
    if getattr(args, "fixture", None):
        argv += ["--fixture", str(args.fixture)]
    ns = build_parser().parse_args(argv)
    # The host-built context is threaded through the registered handler closure
    # for EVERY operational command -- including no-model run-once and the
    # scheduled pipeline -- so native settings validation and every model lane
    # see the same context. This module never constructs one.
    ns.native_ctx = get_native_context()
    # Production entry: with no configuration present, apply neutral generic
    # defaults (no implicit private/legacy sources) rather than the old policy.
    ns.require_config = True
    rc = run_once(ns)
    payload = {"ok": rc == 0, "mode": "check" if getattr(args, "check", False) else "dry-run",
               "fixture": getattr(args, "fixture", None), "rc": rc}
    _emit(payload, as_json=as_json,
          human=f"run-once ({payload['mode']}) completed rc={rc}; nothing was sent.")
    return rc


def pipeline_command(args: argparse.Namespace) -> int:
    """Internal scheduled handler: run the ordinary pipeline in the host context.

    The scheduled pilot runner invokes ``hermes secretary pipeline`` (fixed argv,
    same profile/data paths) so the registered handler closure receives the
    host-built ``ctx`` from native plugin discovery. That is what makes
    ``ctx.llm`` and native ``get_config`` authoritative on the normal schedule;
    the runner never constructs a context itself. A real ``--run`` pipeline is
    used (NOT run-once's dry-run), and stdout remains the single alert line.
    """
    _ensure_src()
    from proactive_pilot.main import build_parser, run_once
    argv = [
        "--state-dir", str(paths.state_dir()),
        "--policy", str(paths.policy_path()),
        "--preferences", str(paths.preferences_path()),
    ]
    if getattr(args, "check", False):
        argv.append("--check")
    if getattr(args, "fixture", None):
        argv += ["--fixture", str(args.fixture)]
    ns = build_parser().parse_args(argv)
    ns.native_ctx = get_native_context()
    # Production entry: a missing configuration must not silently re-enable the
    # legacy private/formal+informal sources.
    ns.require_config = True
    return run_once(ns)


def reminders_command(args: argparse.Namespace) -> int:
    """Registered deterministic reminder handler (no model, no ``ctx.llm``).

    Forwards the host-built native context to ``reminders.main`` so the SAME
    validated per-user configuration precedence as monitoring is authoritative
    for aliases/calendars. The command never builds a context and never requests
    a model; the scheduled no-argv runner enters this handler.
    """
    _ensure_src()
    from proactive_pilot.reminders import main as reminders_main
    rest = [str(a) for a in (getattr(args, "reminder_args", []) or [])]
    argv = [
        "--state-dir", str(paths.state_dir()),
        "--policy", str(paths.policy_path()),
        "--preferences", str(paths.preferences_path()),
        *rest,
    ]
    if not rest or not any(
            a in ("dispatch", "check", "list", "status")
            or a.startswith("--add") or a == "--cancel" or a == "--scheduler-command"
            for a in rest):
        argv.append("dispatch")
    return reminders_main(argv, native_ctx=get_native_context(), require_config=True)


def watch_command(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    _ensure_src()
    from proactive_pilot.main import main as pilot_main
    action = args.action
    if action == "list":
        argv = ["--feedback", "watch-list"]
    elif action == "add":
        if not args.value.strip():
            _emit({"ok": False, "error": "watch add requires a topic"},
                  as_json=as_json, human="watch add requires a topic")
            return 2
        argv = ["--feedback", "watch-add", "--watch-topic", args.value, "--scope", args.scope]
        if args.cadence_minutes is not None:
            argv += ["--minutes", str(args.cadence_minutes)]
    else:  # remove
        if not args.value.strip():
            _emit({"ok": False, "error": "watch remove requires a watch id"},
                  as_json=as_json, human="watch remove requires a watch id")
            return 2
        argv = ["--feedback", "watch-forget", "--id", args.value]
    argv = ["--state-dir", str(paths.state_dir()), "--policy", str(paths.policy_path()),
            "--preferences", str(paths.preferences_path()), *argv]
    return pilot_main(argv)


def feedback_command(args: argparse.Namespace) -> int:
    _ensure_src()
    from proactive_pilot.main import main as pilot_main
    rest = list(getattr(args, "feedback_args", []) or [])
    if not rest:
        _emit({"ok": False, "error": "feedback requires an action (e.g. list, add-explicit)"},
              as_json=bool(getattr(args, "json", False)),
              human="usage: hermes secretary feedback <action> [options]")
        return 2
    argv = ["--state-dir", str(paths.state_dir()), "--policy", str(paths.policy_path()),
            "--preferences", str(paths.preferences_path()), "--feedback", *rest]
    return pilot_main(argv)


# --------------------------------------------------------------------------- #
# configure (validated per-user configuration)
# --------------------------------------------------------------------------- #

def _config_path(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "file", None)
    return Path(explicit) if explicit else paths.user_config_path()


def _write_user_config(path: Path, payload: dict[str, Any], force: bool) -> str:
    import json as _json
    if path.exists() and path.read_text(encoding="utf-8").strip() and not force:
        return "kept"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return "written"


def configure_command(args: argparse.Namespace) -> int:
    """Validated configuration surface: show | validate | preview | init | migrate."""
    as_json = bool(getattr(args, "json", False))
    _ensure_src()
    from proactive_pilot import userconfig
    from proactive_pilot.policy import load_policy

    action = args.action
    path = _config_path(args)

    try:
        if action in ("show", "validate"):
            loaded = userconfig.load_user_config(path, native=get_native_context())
            payload = {
                "ok": True,
                "action": action,
                "path": str(path),
                "schema": userconfig.schema_summary(),
                "config": loaded.summary(),
                "policy_overrides": userconfig.policy_overrides(loaded.config),
                "native": {
                    "context": get_native_context() is not None,
                    "settings_preferred": loaded.source == "native",
                },
            }
            human = (f"config: {'present' if loaded.present else 'defaults'} "
                     f"({loaded.source}, version {loaded.version}); "
                     f"timezone={loaded.config['timezone']}")
            _emit(payload, as_json=as_json, human=human)
            return 0

        if action == "preview":
            loaded = userconfig.load_user_config(path, native=get_native_context())
            base = load_policy(paths.policy_path())
            overrides = userconfig.policy_overrides(loaded.config)
            changes = {k: {"from": base.get(k), "to": v}
                       for k, v in overrides.items() if base.get(k) != v}
            payload = {"ok": True, "action": "preview", "path": str(path),
                       "present": loaded.present, "changes": changes}
            human = (f"config preview: {len(changes)} policy key(s) would change"
                     if loaded.present else "config preview: no user configuration present")
            _emit(payload, as_json=as_json, human=human)
            return 0

        if action == "init":
            example = paths.user_config_example_path()
            if not example.is_file():
                raise FileNotFoundError(f"shipped neutral example missing: {example}")
            payload = json.loads(example.read_text(encoding="utf-8"))
            if getattr(args, "dry_run", False):
                _emit({"ok": True, "action": "init", "dry_run": True, "path": str(path)},
                      as_json=as_json, human=f"config init (dry-run): nothing written to {path}")
                return 0
            status = _write_user_config(path, payload, bool(getattr(args, "force", False)))
            _emit({"ok": True, "action": "init", "status": status, "path": str(path)},
                  as_json=as_json,
                  human=f"config init: {status} {path}")
            return 0

        if action == "migrate":
            src = Path(getattr(args, "from_policy", None) or paths.policy_path())
            if not src.is_file():
                raise FileNotFoundError(f"legacy policy not found: {src}")
            legacy = json.loads(src.read_text(encoding="utf-8"))
            migrated, report = userconfig.migrate_legacy(
                legacy if isinstance(legacy, dict) else {},
                mail_accounts=getattr(args, "mail_account", None),
                calendar_accounts=getattr(args, "calendar_account", None),
                calendar_ids=getattr(args, "calendar_id", None),
                task_lists=getattr(args, "task_list", None),
                task_accounts=getattr(args, "task_account", None),
                task_adapter=getattr(args, "adapter", None),
            )
            migrated = userconfig.validate_config(migrated)
            if getattr(args, "dry_run", False):
                _emit({"ok": True, "action": "migrate", "dry_run": True,
                       "from_policy": str(src), "path": str(path),
                       "report": report,
                       "policy_overrides": userconfig.policy_overrides(migrated)},
                      as_json=as_json, human=f"config migrate (dry-run): reviewed {src}")
                return 0
            status = _write_user_config(path, migrated, bool(getattr(args, "force", False)))
            _emit({"ok": True, "action": "migrate", "status": status,
                   "from_policy": str(src), "path": str(path), "report": report},
                  as_json=as_json,
                  human=f"config migrate: {status} {path} (from {src})")
            return 0
    except userconfig.ConfigError as exc:
        _emit({"ok": False, "action": action, "error": str(exc),
               "remedy": getattr(exc, "remedy", "")},
              as_json=as_json, human=f"configure {action} FAILED: {exc}")
        return 1
    except (OSError, ValueError) as exc:
        _emit({"ok": False, "action": action, "error": f"{type(exc).__name__}: {exc}"},
              as_json=as_json, human=f"configure {action} FAILED: {exc}")
        return 1
    _emit({"ok": False, "error": f"unknown configure action {action!r}"}, as_json=as_json,
          human=f"unknown configure action {action!r}")
    return 2


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

_DISPATCH = {
    "setup": setup_command,
    "status": status_command,
    "pause": pause_command,
    "resume": resume_command,
    "disable": disable_command,
    "uninstall": uninstall_command,
    "self-test": selftest_command,
    "run-once": run_once_command,
    "pipeline": pipeline_command,
    "reminders": reminders_command,
    "watch": watch_command,
    "feedback": feedback_command,
    "configure": configure_command,
    "doctor": doctor_command,
}


def secretary_command(args: argparse.Namespace) -> int:
    _data_override(args)
    sub = getattr(args, "secretary_command", None)
    if not sub:
        print("usage: hermes secretary {setup,status,pause,resume,disable,uninstall,"
              "self-test,run-once,watch,feedback,configure,doctor}")
        return 2
    handler = _DISPATCH.get(sub)
    if handler is None:
        print(f"unknown secretary subcommand: {sub}", file=sys.stderr)
        return 2
    return handler(args)
