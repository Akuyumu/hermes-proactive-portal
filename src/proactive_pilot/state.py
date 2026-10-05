"""Atomic, owner-only, minimal local state.

- State directory is created mode 0700; every JSON file is written 0600 via a
  temp file + os.replace (atomic on POSIX).
- A single advisory flock prevents overlapping collector runs.
- Nothing here persists raw message bodies; callers must pass only refs/keys.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .clock import Clock


class CorruptStateError(RuntimeError):
    """Raised in strict mode when an on-disk artifact cannot be trusted.

    Callers must fail closed rather than silently treating corrupt dedupe or
    policy state as empty (which would re-send suppressed items).
    """


@contextmanager
def file_lock(path: Path) -> Iterator[bool]:
    """Non-blocking single-writer advisory lock for one artifact path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
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


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


class StateStore:
    """Loads/saves bounded JSON artifacts under one private directory."""

    def __init__(self, root: Path, clock: Clock):
        self.root = Path(root)
        self.clock = clock
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    # -- locking ---------------------------------------------------------
    @contextmanager
    def lock(self, name: str = ".lock") -> Iterator[bool]:
        """Non-blocking exclusive run lock. Yields True if acquired."""
        lock_path = self.root / name
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
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

    # -- artifacts -------------------------------------------------------
    def _path(self, name: str) -> Path:
        return self.root / name

    def load(self, name: str, default: dict[str, Any], strict: bool = False) -> dict[str, Any]:
        path = self._path(name)
        if not path.exists():
            return dict(default)
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("artifact is not a JSON object")
            return data
        except (OSError, ValueError) as exc:
            # Corrupt state must never silently reset dedupe/policy. In strict
            # mode the caller fails closed; non-strict returns the default.
            if strict:
                raise CorruptStateError(f"{name}: {exc}") from exc
            return dict(default)

    def save(self, name: str, payload: dict[str, Any]) -> None:
        _atomic_write(self._path(name), payload)

    # -- delivery ledger / dedupe ---------------------------------------
    def load_notify_state(self, strict: bool = False) -> dict[str, Any]:
        return self.load(
            "notify_state.json",
            {"schema": 1, "items": {}, "delivered": [], "health": {}},
            strict=strict,
        )

    def prune_notify_state(self, state: dict[str, Any], retention_days: int) -> dict[str, Any]:
        cutoff = (self.clock.now() - timedelta(days=retention_days)).isoformat()
        items = state.get("items", {})
        state["items"] = {
            key: value
            for key, value in items.items()
            if str(value.get("first_seen", cutoff)) >= cutoff
        }
        delivered = state.get("delivered", [])
        state["delivered"] = [
            row for row in delivered if str(row.get("at", cutoff)) >= cutoff
        ]
        return state


def notification_key(item: dict[str, Any]) -> str:
    """Stable dedupe key: source kind + key + message hash."""
    import hashlib

    raw = f"{item.get('kind','')}|{item.get('key','')}|{item.get('message','')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def iso_now(clock: Clock) -> str:
    return clock.now().isoformat()


def parse_iso(value: str, clock: Clock) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=clock.now().tzinfo)
    return parsed
