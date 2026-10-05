"""Profile-safe path resolution for the hermes-proactive-secretary plugin.

Nothing here hardcodes a personal absolute path. Every location is derived from
an environment override or the active Hermes home:

- ``HERMES_HOME`` selects the active profile home (falling back to
  ``~/.hermes``).
- ``HERMES_PROACTIVE_SECRETARY_DATA`` overrides the owner-only runtime data
  root (state, policy copy, preferences) so tests and the parent installer can
  keep everything inside a scratch directory.
"""
from __future__ import annotations

import os
from pathlib import Path

PLUGIN_NAME = "hermes-proactive-secretary"
DATA_ENV = "HERMES_PROACTIVE_SECRETARY_DATA"


def plugin_root() -> Path:
    """The installed plugin directory (this file's parent)."""
    return Path(__file__).resolve().parent


def hermes_home() -> Path:
    """Active Hermes home: ``$HERMES_HOME`` or ``~/.hermes``."""
    override = os.environ.get("HERMES_HOME")
    if override:
        return Path(override)
    return Path.home() / ".hermes"


def data_root() -> Path:
    """Owner-only runtime data root (survives plugin update/remove)."""
    override = os.environ.get(DATA_ENV)
    if override:
        return Path(override)
    return hermes_home() / "plugin-data" / PLUGIN_NAME


def state_dir() -> Path:
    return data_root() / "var"


def policy_path() -> Path:
    return data_root() / "policy.json"


def preferences_path() -> Path:
    return data_root() / "preferences.json"


def default_policy_path() -> Path:
    return plugin_root() / "policy" / "policy.default.json"


def user_config_example_path() -> Path:
    """The shipped neutral example user configuration (never installed as-is)."""
    return plugin_root() / "policy" / "user-config.example.json"


def user_config_path() -> Path:
    """The validated per-user configuration written by ``configure``.

    It lives beside ``policy.json`` in the owner-only data root; the runtime
    reads it as a sibling of the policy path so scheduled runners resolve the
    same file without hardcoding.
    """
    return data_root() / "user_config.json"


def scripts_dir() -> Path:
    """The native scheduler's script directory: ``$HERMES_HOME/scripts``."""
    return hermes_home() / "scripts"


def runners_source_dir() -> Path:
    """The shipped runner templates that ``setup`` installs from."""
    return plugin_root() / "runners"


def runner_ownership_path() -> Path:
    """Ledger of the runner files this plugin installed and still owns."""
    return data_root() / "runner_ownership.json"


def job_ownership_path() -> Path:
    """Ledger of the native cron jobs this plugin created and still owns.

    Removal is keyed on the recorded job id (not merely its name), so a foreign
    job that happens to share a plugin name can never be removed by accident.
    """
    return data_root() / "cron_job_ownership.json"


def migration_manifest_path() -> Path:
    """Record of the last explicit legacy-state import (hash/counts only)."""
    return data_root() / "migration_manifest.json"


def ensure_data_dirs() -> Path:
    """Create the data + state directories with owner-only ``0700`` mode."""
    root = data_root()
    state = state_dir()
    for path in (root, state):
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return root
