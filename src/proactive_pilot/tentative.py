"""Canonical owner-only tentative draft artifacts, index and notice claims.

Tentative drafts are inferred opportunities, never accepted work. This module is
the single reader/writer/validator used by both the foreground draft producer and
the ordinary main notice path, so neither side invents a different path or trusts
an unverified reference. It reuses the accepted preparation safety primitives
(owner dirs, symlink/no-follow checks, content guards, bounded reads and hashes).
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import preparation as prep
from . import responsibilities as R
from .state import file_lock

TENTATIVE_KEY = "secretary_tentative_drafts"
MAX_TENTATIVE = 12
MAX_TENTATIVE_BYTES = prep.MAX_ARTIFACT_BYTES
TENTATIVE_TTL = timedelta(days=7)
LOCK_NAME = ".tentative.lock"

_REF = re.compile(r"^tentative:([0-9a-f]{24})$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ACTIVE_STATUS = frozenset({"pending", "claimed"})
_TERMINAL_STATUS = frozenset({"expired", "invalid", "unknown", "emitted-unconfirmed"})


class TentativeError(ValueError):
    pass


def _now(now: datetime | None = None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _iso(now: datetime | None = None) -> str:
    return _now(now).isoformat()


def profile_scope() -> str:
    root = Path(prep.default_artifact_root()).absolute()
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def public_ref(artifact_ref: Any) -> str:
    ref = str(artifact_ref or "")
    return ref if _REF.fullmatch(ref) else ""


def _assert_owned_dir(path: Path) -> Path:
    info = os.lstat(str(path))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TentativeError("tentative artifact root is not an owned directory")
    if info.st_uid != os.geteuid():
        raise TentativeError("tentative artifact root is not owned by this user")
    if info.st_mode & 0o077:
        raise TentativeError("tentative artifact root is not owner-only")
    return path


def _assert_owned_file(path: Path, *, expected_hash: str | None = None,
                       expected_size: int | None = None) -> None:
    info = os.lstat(str(path))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TentativeError("tentative artifact is not an owned regular file")
    if info.st_uid != os.geteuid():
        raise TentativeError("tentative artifact is not owned by this user")
    if info.st_mode & 0o077:
        raise TentativeError("tentative artifact is not owner-only")
    if info.st_size > MAX_TENTATIVE_BYTES:
        raise TentativeError("tentative artifact exceeds the size bound")
    if expected_size is not None and info.st_size != expected_size:
        raise TentativeError("tentative artifact size verification failed")
    if expected_hash is not None and not _HASH.fullmatch(str(expected_hash)):
        raise TentativeError("tentative artifact hash is malformed")


def root() -> Path:
    base = Path(prep.default_artifact_root()) / "tentative"
    last: Exception | None = None
    for _ in range(5):
        try:
            return _assert_owned_dir(prep._ensure_owner_dir(base))
        except FileExistsError as exc:
            last = exc
            time.sleep(0.01)
    raise TentativeError("tentative artifact root is unavailable") from last


def digest_for(content: str | bytes) -> tuple[str, bytes]:
    payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return hashlib.sha256(payload).hexdigest(), payload


def ref_for_digest(digest: str) -> str:
    if not _HASH.fullmatch(str(digest)):
        raise TentativeError("tentative artifact digest is malformed")
    return f"tentative:{digest[:24]}"


def filename_for_ref(artifact_ref: Any) -> str:
    match = _REF.fullmatch(str(artifact_ref or ""))
    if not match:
        raise TentativeError("tentative ref must be tentative:<24 lowercase hex>")
    return f"tentative-{match.group(1)}.md"


def paths_for_content(content: str) -> tuple[Path, str]:
    digest, _payload = digest_for(content)
    return root() / filename_for_ref(ref_for_digest(digest)), digest


def path_for_ref(artifact_ref: Any) -> Path:
    name = filename_for_ref(artifact_ref)
    owned_root = root()
    path = owned_root / name
    prep._refuse_symlink(path)
    prep._reject_symlink_ancestors(path)
    try:
        resolved_root = owned_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TentativeError("tentative artifact is missing") from exc
    if resolved.parent != resolved_root:
        raise TentativeError("tentative artifact path escapes the owned root")
    return path


def _verified_bytes(path: Path, expected_hash: str,
                    expected_size: int | None = None) -> bytes:
    prep._refuse_symlink(path)
    prep._reject_symlink_ancestors(path)
    _assert_owned_file(path, expected_hash=expected_hash,
                       expected_size=expected_size)
    data = prep._read_verified(path, expected_hash, expected_size)
    _assert_owned_file(path, expected_hash=expected_hash,
                       expected_size=expected_size)
    return data


@contextmanager
def index_lock(*, wait_seconds: float = 1.0):
    owned_root = root()
    lock_path = owned_root / LOCK_NAME
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    manager = None
    acquired = False
    while True:
        manager = file_lock(lock_path)
        try:
            acquired = bool(manager.__enter__())
        except Exception:
            manager = None
            raise
        if acquired:
            break
        manager.__exit__(None, None, None)
        manager = None
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    try:
        yield acquired
    finally:
        if acquired and manager is not None:
            manager.__exit__(None, None, None)


def validation_record(allowed_refs: Iterable[Any] = (),
                      allowed_facts: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
    refs: list[str] = []
    for item in allowed_refs:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > 240 or _CONTROL.search(text):
            raise TentativeError("supplied evidence ref is malformed")
        if text not in refs:
            refs.append(text)
    facts: list[dict[str, str]] = []
    for item in allowed_facts:
        if not isinstance(item, Mapping):
            continue
        facts.append({"ref": str(item.get("ref") or ""),
                      "text": str(item.get("text") or "")})
    source_ref = refs[0] if refs else ""
    return {
        "evidence_refs": refs,
        "provenance": {"source_ref": source_ref},
        "facts": facts,
    }


def validate_model_refs(value: Any, allowed_refs: Iterable[Any]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TentativeError("draft evidence_refs must be a list")
    allowed = {str(item or "").strip() for item in allowed_refs}
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TentativeError("draft evidence_refs must be strings")
        ref = item.strip()
        if not ref or len(ref) > 240 or _CONTROL.search(ref):
            raise TentativeError("draft evidence_ref is malformed")
        if ref not in allowed:
            raise TentativeError("draft cites an unsupplied evidence ref")
        if ref not in out:
            out.append(ref)
    return out


def _validation_record(validation: Mapping[str, Any] | None,
                       fallback_refs: Iterable[Any] = ()) -> dict[str, Any]:
    if isinstance(validation, Mapping):
        return {
            "evidence_refs": [str(x) for x in validation.get("evidence_refs") or []],
            "provenance": dict(validation.get("provenance") or {}),
            "facts": [dict(x) for x in validation.get("facts") or []
                      if isinstance(x, Mapping)],
        }
    return validation_record(fallback_refs)


def _check_gate(gate: Any) -> None:
    if not callable(gate):
        return
    verdict = gate()
    allowed = True
    reason = "not authorized"
    if isinstance(verdict, tuple):
        allowed = bool(verdict[0]) if verdict else False
        reason = str(verdict[1])[:160] if len(verdict) > 1 else reason
    elif verdict is False:
        allowed = False
    if not allowed:
        raise TentativeError(f"tentative preparation is not authorized: {reason}")


def _check_title(title: Any, rec: dict[str, Any]) -> str:
    label = str(title or "Tentative private preparation").strip()
    if not label or len(label) > prep._MAX_TITLE or _CONTROL.search(label):
        raise TentativeError("tentative title is empty/oversized/unsafe")
    prep._validate_content(label, ext=".md", rec=rec)
    return label


def _write_file(path: Path, payload: bytes, digest: str) -> bool:
    if os.path.lexists(str(path)):
        _verified_bytes(path, digest, len(payload))
        return False
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-",
                                    suffix=".md")
    tmp = Path(tmp_name)
    created = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        _verified_bytes(tmp, digest, len(payload))
        try:
            os.link(str(tmp), str(path))
            created = True
        except FileExistsError:
            _verified_bytes(path, digest, len(payload))
    except Exception:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    _verified_bytes(path, digest, len(payload))
    return True


def _row_from_payload(*, artifact_ref: str, digest: str, size: int,
                      evidence_refs: Iterable[str], now: datetime) -> dict[str, Any]:
    stamp = _now(now)
    return {
        "at": stamp.isoformat(),
        "artifact_ref": artifact_ref,
        "hash": digest,
        "size": int(size),
        "profile": profile_scope(),
        "scope": "conversation",
        "status": "tentative",
        "not_accepted": True,
        "evidence_refs": list(evidence_refs),
        "expires_at": (stamp + TENTATIVE_TTL).isoformat(),
        "notified_at": "",
        "notification": {"status": "pending", "at": "", "token": ""},
    }


def validate_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise TentativeError("tentative index contains a malformed row")
    copied = dict(row)
    ref = str(copied.get("artifact_ref") or "")
    if not _REF.fullmatch(ref):
        raise TentativeError("tentative index row has an invalid artifact ref")
    digest = str(copied.get("hash") or "")
    if not _HASH.fullmatch(digest):
        raise TentativeError("tentative index row is missing a valid hash")
    try:
        size = int(copied.get("size"))
    except (TypeError, ValueError) as exc:
        raise TentativeError("tentative index row is missing a valid size") from exc
    if size < 0 or size > MAX_TENTATIVE_BYTES:
        raise TentativeError("tentative index row size is out of bounds")
    if str(copied.get("profile") or "") != profile_scope():
        raise TentativeError("tentative index row belongs to another profile")
    if str(copied.get("scope") or "") != "conversation":
        raise TentativeError("tentative index row has an unsupported scope")
    if str(copied.get("status") or "") != "tentative" or copied.get("not_accepted") is not True:
        raise TentativeError("tentative index row is not clearly tentative")
    try:
        datetime.fromisoformat(str(copied.get("at") or ""))
        datetime.fromisoformat(str(copied.get("expires_at") or ""))
    except ValueError as exc:
        raise TentativeError("tentative index row has invalid timestamps") from exc
    notification = copied.get("notification")
    if notification is None:
        notification = {}
    if not isinstance(notification, Mapping):
        raise TentativeError("tentative index row notification is malformed")
    copied["notification"] = {
        "status": str(notification.get("status") or "pending"),
        "at": str(notification.get("at") or ""),
        "token": str(notification.get("token") or ""),
    }
    copied["notified_at"] = str(copied.get("notified_at") or "")
    refs = copied.get("evidence_refs")
    if refs is not None and not isinstance(refs, list):
        raise TentativeError("tentative index row evidence_refs is malformed")
    return copied


def read_index(facade: Any) -> list[dict[str, Any]]:
    if facade is None:
        raise TentativeError("tentative drafts require an owned state facade")
    try:
        raw = facade.get(TENTATIVE_KEY)
    except Exception as exc:  # noqa: BLE001 - unknown index state fails closed
        raise TentativeError(
            f"tentative index unavailable: {type(exc).__name__}") from exc
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TentativeError("tentative index is malformed")
    if len(raw) > MAX_TENTATIVE:
        raise TentativeError("tentative index exceeds the queue bound")
    return [validate_row(row) for row in raw]


def write_index(facade: Any, rows: list[dict[str, Any]]) -> None:
    if facade is None:
        raise TentativeError("tentative drafts require an owned state facade")
    try:
        facade.set(TENTATIVE_KEY, rows)
    except Exception as exc:  # noqa: BLE001 - no stale/partial index survives
        raise TentativeError(
            f"tentative index write failed: {type(exc).__name__}") from exc


def _find_ref(rows: list[dict[str, Any]], artifact_ref: str) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get("artifact_ref") or "") == artifact_ref:
            return row
    return None


def write_draft(facade: Any, *, title: Any, content: Any,
                validation: Mapping[str, Any] | None = None,
                evidence_refs: Iterable[Any] = (),
                gate: Any = None, now: datetime | None = None) -> dict[str, Any]:
    if facade is None:
        raise TentativeError("tentative drafts require an owned state facade")
    if not isinstance(content, str) or not content.strip():
        raise TentativeError("tentative draft was empty")
    rec = _validation_record(validation)
    allowed = [str(x) for x in (rec.get("evidence_refs") or [])]
    supplied = validate_model_refs(list(evidence_refs or []), allowed)
    label = _check_title(title, rec)
    payload = prep._validate_content(content, ext=".md", rec=rec)
    digest, payload = digest_for(payload)
    artifact_ref = ref_for_digest(digest)
    stamp = _now(now)

    with index_lock() as acquired:
        if not acquired:
            raise TentativeError("tentative index is locked by another writer")
        rows = read_index(facade)
        existing = _find_ref(rows, artifact_ref)
        if existing is not None:
            _verified_bytes(path_for_ref(artifact_ref), str(existing["hash"]),
                            int(existing["size"]))
            return dict(existing)
        if len(rows) >= MAX_TENTATIVE:
            raise TentativeError("tentative draft queue is full; review or discard one first")
        _check_gate(gate)
        record = _row_from_payload(
            artifact_ref=artifact_ref, digest=digest, size=len(payload),
            evidence_refs=supplied or allowed, now=stamp)
        path = root() / filename_for_ref(artifact_ref)
        prep._refuse_symlink(path)
        prep._reject_symlink_ancestors(path)
        created = _write_file(path, payload, digest)
        try:
            _check_gate(gate)
            rows.append(record)
            write_index(facade, rows)
        except Exception as exc:  # noqa: BLE001 - never leave an unindexed artifact
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
            if isinstance(exc, TentativeError):
                raise
            raise TentativeError(
                f"tentative index write failed: {type(exc).__name__}") from exc
        return dict(record)


def _expired(row: Mapping[str, Any], now: datetime) -> bool:
    try:
        expires = datetime.fromisoformat(str(row.get("expires_at") or ""))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    stamp = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return stamp >= expires


def _verify_artifact(row: Mapping[str, Any]) -> None:
    artifact_ref = str(row.get("artifact_ref") or "")
    path = path_for_ref(artifact_ref)
    data = _verified_bytes(path, str(row.get("hash") or ""),
                           int(row.get("size")))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TentativeError("tentative artifact is not UTF-8 text") from exc
    if _CONTROL.search(text):
        raise TentativeError("tentative artifact contains control characters")


def claim_notice(facade: Any, *, policy: Mapping[str, Any],
                 now: datetime | None = None,
                 allowed: bool = True) -> dict[str, Any] | None:
    if not allowed or facade is None:
        return None
    scopes = {str(scope).strip().lower()
              for scope in (policy.get("preparation_scopes") or [])}
    if "conversation" not in scopes:
        return None
    stamp = _now(now)
    with index_lock() as acquired:
        if not acquired:
            return None
        try:
            rows = read_index(facade)
        except TentativeError:
            return None
        changed = False
        for index, row in enumerate(rows):
            notification = dict(row.get("notification") or {})
            status = str(notification.get("status") or "pending")
            if row.get("notified_at") or status not in _ACTIVE_STATUS:
                continue
            if status == "claimed":
                continue
            if _expired(row, stamp):
                notification.update({"status": "expired", "at": stamp.isoformat()})
                row["notification"] = notification
                row["notified_at"] = stamp.isoformat()
                rows[index] = row
                changed = True
                continue
            try:
                _verify_artifact(row)
            except Exception:  # noqa: BLE001 - invalid/stale ref never notifies
                notification.update({"status": "invalid", "at": stamp.isoformat()})
                row["notification"] = notification
                row["notified_at"] = stamp.isoformat()
                rows[index] = row
                changed = True
                continue
            token = secrets.token_hex(16)
            notification.update({"status": "claimed", "token": token,
                                 "at": stamp.isoformat()})
            row["notification"] = notification
            row["notified_at"] = stamp.isoformat()
            rows[index] = row
            write_index(facade, rows)
            return dict(row)
        if changed:
            write_index(facade, rows)
    return None


def mark_notice(facade: Any, claim: Mapping[str, Any], *,
                status: str, now: datetime | None = None) -> dict[str, Any]:
    if status not in {"unknown", "emitted-unconfirmed"}:
        raise TentativeError("unsupported tentative notice status")
    artifact_ref = str(claim.get("artifact_ref") or "")
    token = str((claim.get("notification") or {}).get("token")
                or claim.get("notice_token") or "")
    stamp = _iso(now)
    with index_lock() as acquired:
        if not acquired:
            raise TentativeError("tentative index is locked by another writer")
        rows = read_index(facade)
        row = _find_ref(rows, artifact_ref)
        if row is None:
            raise TentativeError("tentative claim no longer exists")
        notification = dict(row.get("notification") or {})
        if token and str(notification.get("token") or "") != token:
            raise TentativeError("tentative claim token does not match")
        notification.update({"status": status, "at": stamp})
        row["notification"] = notification
        if not row.get("notified_at"):
            row["notified_at"] = stamp
        write_index(facade, rows)
        return dict(row)


__all__ = ["LOCK_NAME", "MAX_TENTATIVE", "MAX_TENTATIVE_BYTES", "TENTATIVE_KEY",
           "TENTATIVE_TTL", "TentativeError", "claim_notice", "digest_for",
           "filename_for_ref", "index_lock", "mark_notice", "path_for_ref",
           "paths_for_content", "profile_scope", "public_ref", "read_index",
           "ref_for_digest", "root", "validate_model_refs", "validate_row",
           "validation_record", "write_draft", "write_index"]
