"""Thin, testable wrapper over the Hermes native cron surface.

The plugin owns exactly two jobs (a 15-minute pilot and a 1-minute requested
reminder dispatcher). This module speaks to the installed ``hermes cron`` CLI
through a small :class:`CronBackend` protocol so the CLI logic can be unit
tested without a live scheduler.

Real surface (parent-verified on the target host; overridable per verb via env
for a differently spelled release)::

    hermes cron create <schedule> [prompt] [--name NAME] [--script SCRIPT]
                       [--no-agent] [--deliver TARGET] [--workdir DIR]
    hermes cron list [--all]        # TEXT OUTPUT ONLY -- no --json flag
    hermes cron remove <job_id>     # POSITIONAL job id, not a name

The schedule is positional. Script jobs use ``--script`` (a filename under the
profile's scripts directory) together with ``--no-agent``; there is no
``--command`` flag and no JSON output.

Hard rules:
- The backend is only touched from explicit user commands (``setup`` /
  ``disable`` / ``uninstall``), never at import or ``register()`` time.
- Every mutation is verified by listing jobs back; a claimed removal that is
  still present is reported as a failure, never silently swallowed.
- Nothing here installs, enables, or publishes anything.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

PILOT_SCRIPT = "proactive_secretary_runner.py"
REMINDER_SCRIPT = "proactive_secretary_reminder_runner.py"

# Namespaced to this plugin so a pre-existing unrelated job of a generic name can
# never be matched (and therefore never removed) by accident.
PILOT_JOB_NAME = "hermes-proactive-secretary-pilot"
REMINDER_JOB_NAME = "hermes-proactive-secretary-reminders"
OWNED_JOB_NAMES: tuple[str, ...] = (PILOT_JOB_NAME, REMINDER_JOB_NAME)
OWNED_PLUGIN = "hermes-proactive-secretary"
JOB_OWNERSHIP_SCHEMA = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CronError(RuntimeError):
    """The Hermes cron surface could not be reached or used safely."""


@dataclass
class CronJobSpec:
    """Declarative description of one native cron job this plugin owns."""

    name: str
    schedule: str
    script: str
    deliver: str = "local"
    workdir: str = ""
    description: str = ""
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class CronJobResult:
    name: str
    action: str  # created | already-present | updated | removed | missing
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "action": self.action, "detail": self.detail}


class CronBackend(Protocol):
    def is_available(self) -> bool: ...
    def list_jobs(self) -> list[dict[str, Any]]: ...
    def add_job(self, spec: CronJobSpec) -> dict[str, Any]: ...
    def remove_job(self, job_id: str) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# Text parsing (``hermes cron list`` prints a table of job blocks)
# --------------------------------------------------------------------------- #

_JOB_HEADER = re.compile(r"^\s*(\S+)\s+\[(\w+)\]\s*$")
_FIELD = re.compile(r"^\s+([A-Za-z][A-Za-z ]*?):\s*(.*?)\s*$")
_FIELD_KEYS = {
    "name": "name",
    "schedule": "schedule",
    "next run": "next_run",
    "deliver": "deliver",
    "script": "script",
    "mode": "mode",
    "workdir": "workdir",
    "last run": "last_run",
}


def parse_cron_list(text: str) -> list[dict[str, Any]]:
    """Parse the text table printed by ``hermes cron list`` into job dicts.

    Each block is headed by ``<job_id> [active|paused]`` followed by indented
    ``Field: value`` lines. Unrecognised text is ignored rather than guessed at.
    """
    jobs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in (text or "").splitlines():
        header = _JOB_HEADER.match(line)
        if header:
            if current is not None:
                jobs.append(current)
            current = {"id": header.group(1), "state": header.group(2)}
            continue
        if current is None:
            continue
        match = _FIELD.match(line)
        if not match:
            continue
        mapped = _FIELD_KEYS.get(match.group(1).strip().lower())
        if mapped:
            current[mapped] = match.group(2).strip()
    if current is not None:
        jobs.append(current)
    return jobs


def _job_id(job: dict[str, Any]) -> str:
    return str(job.get("id") or job.get("job_id") or "")


def _find_job(jobs: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((j for j in jobs if str(j.get("name") or "") == name), None)


def _same_job(job: dict[str, Any], spec: CronJobSpec) -> bool:
    if str(job.get("schedule") or "") != spec.schedule:
        return False
    if str(job.get("script") or "") != spec.script:
        return False
    if str(job.get("deliver") or "") != spec.deliver:
        return False
    if spec.workdir and str(job.get("workdir") or "") != spec.workdir:
        return False
    return True


# --------------------------------------------------------------------------- #
# Job-ownership ledger (names are NOT proof of ownership)
# --------------------------------------------------------------------------- #

def load_job_ownership(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Load the plugin's own record of the jobs it created.

    The record maps the plugin's job name -> ``{id, name, created_at, ...}``. It
    is the only authority for removal; a same-named job absent from the ledger is
    treated as foreign and is never adopted, overwritten, or removed.
    """
    data: dict[str, Any] = {}
    if path is not None:
        try:
            loaded = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        jobs = {}
    return {"schema": JOB_OWNERSHIP_SCHEMA, "plugin": OWNED_PLUGIN, "jobs": jobs}


def save_job_ownership(path: str | os.PathLike[str] | None, data: dict[str, Any]) -> None:
    if path is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, target)


def owned_job_record(ownership: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if not isinstance(ownership, dict):
        return None
    record = (ownership.get("jobs") or {}).get(name)
    return record if isinstance(record, dict) else None


def record_owned_job(ownership: dict[str, Any], spec: CronJobSpec, job: dict[str, Any],
                     created_at: str | None = None) -> dict[str, Any]:
    record = {
        "id": _job_id(job),
        "name": spec.name,
        "created_at": created_at or _now_iso(),
        "schedule": spec.schedule,
        "script": spec.script,
        "deliver": spec.deliver,
        "workdir": spec.workdir,
    }
    ownership.setdefault("jobs", {})[spec.name] = record
    return record



# --------------------------------------------------------------------------- #
# Default backend: shell out to ``hermes cron``
# --------------------------------------------------------------------------- #

class HermesCronBackend:
    """Default backend: shell out to ``hermes cron`` using the real verb shape.

    The three verbs default to ``create`` / ``list`` / ``remove`` and may be
    overridden (for a differently spelled release) with
    ``HERMES_CRON_CREATE_VERB`` / ``HERMES_CRON_LIST_VERB`` /
    ``HERMES_CRON_REMOVE_VERB``. ``HERMES_BIN`` overrides the executable.
    """

    def __init__(
        self,
        hermes_bin: str | None = None,
        *,
        runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        timeout: int = 60,
        create_verb: str | None = None,
        list_verb: str | None = None,
        remove_verb: str | None = None,
    ) -> None:
        self.hermes_bin = hermes_bin or os.environ.get("HERMES_BIN") or "hermes"
        self._runner = runner
        self.timeout = timeout
        self.create_verb = create_verb or os.environ.get("HERMES_CRON_CREATE_VERB") or "create"
        self.list_verb = list_verb or os.environ.get("HERMES_CRON_LIST_VERB") or "list"
        self.remove_verb = remove_verb or os.environ.get("HERMES_CRON_REMOVE_VERB") or "remove"

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        if self._runner is not None:
            return self._runner(argv)
        return subprocess.run(
            argv, shell=False, capture_output=True, text=True,
            timeout=self.timeout, check=False,
        )

    def _cron_argv(self, *args: str) -> list[str]:
        return [self.hermes_bin, "cron", *args]

    def is_available(self) -> bool:
        try:
            proc = self._run(self._cron_argv("--help"))
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def list_jobs(self) -> list[dict[str, Any]]:
        try:
            proc = self._run(self._cron_argv(self.list_verb, "--all"))
        except (OSError, subprocess.SubprocessError) as exc:
            raise CronError(f"cannot list cron jobs: {exc}") from exc
        if proc.returncode != 0:
            raise CronError(f"cron list failed (exit {proc.returncode}): {(proc.stderr or '').strip()[:200]}")
        return parse_cron_list(proc.stdout)

    def add_job(self, spec: CronJobSpec) -> dict[str, Any]:
        argv = self._cron_argv(
            self.create_verb, spec.schedule,
            "--name", spec.name,
            "--script", spec.script,
            "--no-agent",
            "--deliver", spec.deliver,
        )
        if spec.workdir:
            argv += ["--workdir", spec.workdir]
        try:
            proc = self._run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CronError(f"cannot add cron job {spec.name!r}: {exc}") from exc
        if proc.returncode != 0:
            raise CronError(
                f"cron create {spec.name!r} failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:240]}"
            )
        return {"name": spec.name, "created": True, "argv": argv}

    def remove_job(self, job_id: str) -> dict[str, Any]:
        try:
            proc = self._run(self._cron_argv(self.remove_verb, str(job_id)))
        except (OSError, subprocess.SubprocessError) as exc:
            raise CronError(f"cannot remove cron job {job_id!r}: {exc}") from exc
        if proc.returncode != 0:
            raise CronError(
                f"cron remove {job_id!r} failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:240]}"
            )
        return {"id": job_id, "removed": True}


# --------------------------------------------------------------------------- #
# Idempotent, verified helpers
# --------------------------------------------------------------------------- #

def ensure_job(backend: CronBackend, spec: CronJobSpec, *,
               ownership: dict[str, Any] | None = None,
               ownership_path: str | os.PathLike[str] | None = None,
               now: str | None = None) -> CronJobResult:
    """Idempotently create ``spec``; re-create it if its shape changed.

    When ``ownership`` is supplied, ownership is enforced: a same-named job that
    was NOT recorded by this plugin is refused (never adopted or overwritten),
    and every created/updated job's id is recorded. Without ``ownership`` the
    legacy name-based behaviour is preserved for direct callers/tests.
    """
    jobs = backend.list_jobs()
    existing = _find_job(jobs, spec.name)
    record = owned_job_record(ownership, spec.name) if ownership is not None else None

    if existing is not None:
        job_id = _job_id(existing)
        if ownership is not None and (record is None or str(record.get("id") or "") != job_id):
            raise CronError(
                f"refusing to adopt foreign cron job {spec.name!r} (id={job_id or '?'}): "
                "this plugin did not create it. Remove or rename the conflicting job, "
                "then re-run setup."
            )
        if _same_job(existing, spec):
            return CronJobResult(spec.name, "already-present", "unchanged")
        if not job_id:
            raise CronError(f"cron job {spec.name!r} has no id to remove")
        backend.remove_job(job_id)
        if (next((j for j in backend.list_jobs() if _job_id(j) == job_id), None) is not None
                or _find_job(backend.list_jobs(), spec.name) is not None):
            raise CronError(f"cron job {spec.name!r} did not disappear before update")
        backend.add_job(spec)
        updated = _find_job(backend.list_jobs(), spec.name)
        if updated is None:
            raise CronError(f"cron job {spec.name!r} did not reappear after update")
        if ownership is not None:
            record_owned_job(ownership, spec, updated, created_at=(record or {}).get("created_at") or now)
            save_job_ownership(ownership_path, ownership)
        return CronJobResult(spec.name, "updated", "shape changed; recreated")

    backend.add_job(spec)
    created = _find_job(backend.list_jobs(), spec.name)
    if created is None:
        raise CronError(f"cron job {spec.name!r} was not confirmed after creation")
    if ownership is not None:
        record_owned_job(ownership, spec, created, created_at=now)
        save_job_ownership(ownership_path, ownership)
    return CronJobResult(spec.name, "created")


def remove_job(backend: CronBackend, name: str, *,
               ownership: dict[str, Any] | None = None,
               ownership_path: str | os.PathLike[str] | None = None) -> CronJobResult:
    """Remove only a job this plugin recorded (id AND name must match).

    With ``ownership``: a same-named but unrecorded job is foreign and is never
    removed; a recorded id that no longer exists is a harmless no-op. Without
    ``ownership`` the legacy name-based removal is preserved.
    """
    jobs = backend.list_jobs()
    match = _find_job(jobs, name)

    if ownership is None:
        if match is None:
            return CronJobResult(name, "missing", "not present")
        job_id = _job_id(match)
        if not job_id:
            raise CronError(f"cron job {name!r} has no id to remove")
        backend.remove_job(job_id)
        if _find_job(backend.list_jobs(), name) is not None:
            raise CronError(f"cron job {name!r} is still present after removal")
        return CronJobResult(name, "removed", f"id={job_id}")

    record = owned_job_record(ownership, name)
    if record is None:
        if match is not None:
            return CronJobResult(
                name, "foreign",
                f"left untouched: foreign job id={_job_id(match) or '?'} shares the name; "
                "this plugin did not create it",
            )
        return CronJobResult(name, "missing", "no owned job recorded and none present")

    recorded_id = str(record.get("id") or "")
    owned_match = next(
        (j for j in jobs if _job_id(j) == recorded_id and str(j.get("name") or "") == name),
        None,
    )
    if owned_match is None:
        ownership["jobs"].pop(name, None)
        save_job_ownership(ownership_path, ownership)
        if match is not None:
            return CronJobResult(
                name, "foreign",
                "left untouched: the recorded job is gone but a foreign job now uses the name",
            )
        return CronJobResult(name, "missing", "recorded job no longer present")
    if not recorded_id:
        raise CronError(f"cron job {name!r} has no recorded id to remove")
    backend.remove_job(recorded_id)
    if next((j for j in backend.list_jobs() if _job_id(j) == recorded_id), None) is not None:
        raise CronError(f"cron job {name!r} (id={recorded_id}) is still present after removal")
    ownership["jobs"].pop(name, None)
    save_job_ownership(ownership_path, ownership)
    return CronJobResult(name, "removed", f"id={recorded_id}")


def classify_jobs(jobs: list[dict[str, Any]], ownership: dict[str, Any] | None) -> dict[str, Any]:
    """Split the plugin's job names into owned / foreign / missing.

    "No jobs of ours" (``state == "none"``) is distinct from "a foreign job
    happens to share a name" (``state`` in ``{"foreign", "mixed"}``).
    """
    owned: list[dict[str, Any]] = []
    foreign: list[dict[str, Any]] = []
    missing: list[str] = []
    for name in OWNED_JOB_NAMES:
        record = owned_job_record(ownership, name)
        by_id: dict[str, Any] | None = None
        if record:
            recorded_id = str(record.get("id") or "")
            by_id = next(
                (j for j in jobs if _job_id(j) == recorded_id and str(j.get("name") or "") == name),
                None,
            )
        by_name = _find_job(jobs, name)
        if by_id is not None:
            owned.append({"id": _job_id(by_id), "name": name, "state": str(by_id.get("state") or "")})
        elif by_name is not None:
            foreign.append({"id": _job_id(by_name), "name": name,
                            "state": str(by_name.get("state") or "")})
        elif record:
            missing.append(name)
    if owned and foreign:
        state = "mixed"
    elif foreign:
        state = "foreign"
    elif owned:
        state = "owned"
    elif missing:
        state = "missing"
    else:
        state = "none"
    return {"state": state, "owned": owned, "foreign": foreign, "missing": missing,
            "jobs": owned}



class InMemoryCronBackend:
    """Deterministic backend for tests (and a safe ``--no-cron`` dry inspection)."""

    def __init__(self, jobs: list[dict[str, Any]] | None = None) -> None:
        self.jobs: list[dict[str, Any]] = [dict(j) for j in (jobs or [])]
        self._counter = 0
        self.fail_remove = False

    def is_available(self) -> bool:
        return True

    def list_jobs(self) -> list[dict[str, Any]]:
        return [dict(j) for j in self.jobs]

    def add_job(self, spec: CronJobSpec) -> dict[str, Any]:
        self._counter += 1
        job = {
            "id": f"job-{self._counter}",
            "name": spec.name,
            "schedule": spec.schedule,
            "script": spec.script,
            "deliver": spec.deliver,
            "workdir": spec.workdir,
            "mode": "no-agent",
            "state": "active",
            "last_run": "",
        }
        self.jobs.append(job)
        return job

    def remove_job(self, job_id: str) -> dict[str, Any]:
        if self.fail_remove:
            return {"id": job_id, "removed": False}
        self.jobs = [j for j in self.jobs if _job_id(j) != str(job_id)]
        return {"id": job_id, "removed": True}


def build_job_specs(*, deliver: str = "local", workdir: str = "") -> list[CronJobSpec]:
    """Build the two canonical jobs this plugin owns."""
    return [
        CronJobSpec(
            name=PILOT_JOB_NAME,
            schedule="*/15 * * * *",
            script=PILOT_SCRIPT,
            deliver=deliver,
            workdir=workdir,
            description="Proactive secretary collector + evaluator (every 15 minutes)",
        ),
        CronJobSpec(
            name=REMINDER_JOB_NAME,
            schedule="* * * * *",
            script=REMINDER_SCRIPT,
            deliver=deliver,
            workdir=workdir,
            description="Requested-reminder dispatcher (every 1 minute)",
        ),
    ]
