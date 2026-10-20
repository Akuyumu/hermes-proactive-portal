"""Reproducible code-only 0.5.0 export builder and installed-candidate doctor.

This is not a new package manager. It uses the plugin's existing manifest and
Python modules, applies the same export rules the release review requires, and
uses the standard library only. It never installs, enables, publishes or touches
a live profile.

Rules enforced by :func:`build_package`:

* every shipped file comes from an explicit allowlist of code/docs/policy/skill/
  runner trees (no ``.build-context``, tests, git metadata, caches, private
  runtime state or scratch files);
* the manifest and ``src/proactive_pilot/__init__.py`` both say 0.5.0;
* sanitisation terms and absolute personal home paths are rejected;
* file order, timestamps and hashes are deterministic.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
VERSION = "0.5.0"
SHIPPED_DIRS = ("src", "policy", "skills", "runners", "public_samples", "fixtures")
ROOT_SUFFIXES = (".py", ".md", ".json", ".yaml", ".yml", ".html", ".txt")
# Extensionless root files that are part of the redistribution contract. The
# README links LICENSE, so omitting it would ship a broken/incomplete package.
ROOT_NAMES = ("LICENSE", "NOTICE")
REQUIRED_ROOT_NAMES = ("LICENSE",)
EXCLUDED_NAMES = {
    ".git", ".gitignore", ".build-context", "tests", "var", "__pycache__",
    ".mypy_cache", ".pytest_cache", "references",
}
# Obsolete 0.4.0 finalization notes stay in the source tree for history but are
# NOT shipped as 0.5.0 documentation.
EXCLUDED_ROOT_FILES = {"FINALIZATION_PREPARATION.md"}
FORBIDDEN = ("Kuyu", "-sama")
HOME_RE = re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")


def _shipped_files(root: Path = ROOT) -> list[Path]:
    files: list[Path] = []
    for path in root.iterdir():
        if path.name == "release_export.py" or path.name in EXCLUDED_ROOT_FILES:
            continue
        if path.is_file() and (path.suffix.lower() in ROOT_SUFFIXES
                               or path.name in ROOT_NAMES):
            files.append(path)
    for name in SHIPPED_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            parts = set(path.relative_to(root).parts)
            if parts & EXCLUDED_NAMES or "__pycache__" in parts:
                continue
            files.append(path)
    return sorted(set(files), key=lambda p: p.relative_to(root).as_posix())


def _version(root: Path) -> tuple[str, str]:
    manifest = (root / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r"(?m)^version:\s*(\S+)\s*$", manifest)
    module = (root / "src" / "proactive_pilot" / "__init__.py").read_text(encoding="utf-8")
    module_match = re.search(r'__version__\s*=\s*"([^"]+)"', module)
    return (match.group(1) if match else "",
            module_match.group(1) if module_match else "")


def scan(root: Path = ROOT) -> dict[str, Any]:
    problems: list[str] = []
    manifest_version, module_version = _version(root)
    if manifest_version != VERSION:
        problems.append(f"plugin.yaml version is {manifest_version!r}, expected {VERSION}")
    if module_version != VERSION:
        problems.append(f"module version is {module_version!r}, expected {VERSION}")
    files = _shipped_files(root)
    for name in REQUIRED_ROOT_NAMES:
        path = root / name
        if not path.is_file() or not path.read_bytes().strip():
            problems.append(f"required root file {name} is missing or empty")
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for term in FORBIDDEN:
            if term in text:
                problems.append(f"{path.relative_to(root)} contains forbidden term {term!r}")
        for match in HOME_RE.finditer(text):
            problems.append(f"{path.relative_to(root)} contains personal path {match.group(0)}")
    return {"ok": not problems, "version": VERSION, "file_count": len(files),
            "files": [p.relative_to(root).as_posix() for p in files],
            "problems": problems}


def build_package(output: Path, *, root: Path = ROOT) -> dict[str, Any]:
    """Build a byte-reproducible code-only archive.

    Every tar entry gets fixed metadata and stable order, and the gzip wrapper
    pins ``mtime=0`` with no filename header. ``sha256`` is the SHA-256 of the
    actual archive bytes (never a mislabeled contents manifest).
    """
    report = scan(root)
    if not report["ok"]:
        raise ValueError("export scan failed: " + "; ".join(report["problems"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    contents = hashlib.sha256()
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, filename="") as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in _shipped_files(root):
                rel = path.relative_to(root).as_posix()
                data = path.read_bytes()
                info = tarfile.TarInfo(rel)
                info.size = len(data)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mode = 0o644
                info.type = tarfile.REGTYPE
                info.pax_headers = {}
                archive.addfile(info, io.BytesIO(data))
                contents.update(rel.encode("utf-8"))
                contents.update(data)
    payload = buffer.getvalue()
    output.write_bytes(payload)
    return {"ok": True, "version": VERSION, "package": str(output),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "contents_sha256": contents.hexdigest(),
            "bytes": len(payload), "file_count": report["file_count"]}


def _import_registration(installed: Path) -> dict[str, Any]:
    """Import and REGISTER the actual extracted entrypoints in isolation."""
    with tempfile.TemporaryDirectory(prefix="secretary-installed-") as tmp:
        env = dict(os.environ, HOME=str(Path(tmp) / "home"),
                   HERMES_HOME=str(Path(tmp) / "profile"),
                   TMPDIR=str(Path(tmp)), PYTHONDONTWRITEBYTECODE="1")
        (Path(tmp) / "home").mkdir()
        (Path(tmp) / "profile").mkdir()
        code = f"""
import importlib, importlib.util, sys
from pathlib import Path
root = Path({str(installed)!r})
assert (root / "LICENSE").is_file(), "LICENSE missing from extracted package"
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location(
    "hermes_proactive_secretary_installed", root / "__init__.py",
    submodule_search_locations=[str(root)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert callable(getattr(module, "register", None)), "register() missing"
sys.path.insert(0, str(root / "src"))
import proactive_pilot.preparation as prep
import proactive_pilot.native_runtime as runtime
pkg = spec.name
cli = importlib.import_module(pkg + ".secretary_cli")
runners = importlib.import_module(pkg + ".runner_install")
assert callable(getattr(prep, "preflight", None))
assert (root / "runners" / "proactive_secretary_prepare_runner.py").is_file()
print("ok")
"""
        try:
            proc = subprocess.run([sys.executable, "-c", code], env=env,
                                  capture_output=True, text=True, timeout=60)
        except Exception as exc:  # noqa: BLE001 - report, never crash the doctor
            return {"ok": False, "reason": type(exc).__name__}
        return {"ok": proc.returncode == 0 and "ok" in (proc.stdout or ""),
                "returncode": proc.returncode,
                "detail": (proc.stderr or proc.stdout or "")[-400:]}


def _native_host_doctor(installed: Path) -> dict[str, Any]:
    """Run the canonical native plugin doctor against an isolated profile."""
    hermes = shutil.which("hermes")
    if not hermes:
        return {"available": False, "ok": False,
                "reason": "canonical hermes CLI is not on PATH"}
    with tempfile.TemporaryDirectory(prefix="secretary-doctor-") as tmp:
        home = Path(tmp) / "home"
        profile = Path(tmp) / "profile"
        home.mkdir()
        profile.mkdir()
        env = dict(os.environ, HOME=str(home), HERMES_HOME=str(profile),
                   TMPDIR=str(Path(tmp)), PYTHONDONTWRITEBYTECODE="1")
        try:
            proc = subprocess.run([hermes, "plugins", "doctor", "--ci", str(installed)],
                                  env=env, capture_output=True, text=True,
                                  timeout=60)
        except Exception as exc:  # noqa: BLE001
            return {"available": True, "ok": False, "reason": type(exc).__name__}
        return {"available": True, "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "detail": (proc.stdout or proc.stderr or "")[-600:]}


def doctor(installed: Path) -> dict[str, Any]:
    """Installed-candidate validation; this is NOT a live host doctor itself.

    It performs the package-local checks, then imports/registers the actual
    extracted entrypoints and finally delegates host validation to the canonical
    ``hermes plugins doctor --ci`` under a synthetic offline profile.
    """
    installed = Path(installed)
    required = ("plugin.yaml", "__init__.py", "secretary_cli.py", "paths.py",
                "LICENSE", "src/proactive_pilot/__init__.py",
                "runners/proactive_secretary_prepare_runner.py")
    missing = [name for name in required if not (installed / name).is_file()]
    manifest_version, module_version = _version(installed)
    shipped = scan(installed)
    server_assets = all((installed / "runners" / name).is_file() for name in (
        "proactive_secretary_runner.py", "proactive_secretary_reminder_runner.py",
        "proactive_secretary_prepare_runner.py"))
    imports = _import_registration(installed) if not missing else {
        "ok": False, "reason": "required files missing"}
    # An incomplete package cannot be a valid host candidate. Running the native
    # CLI doctor on it only adds an unbounded/expensive startup and cannot change
    # the verdict, so fail fast on the package-local result instead.
    host = ({"available": False, "ok": False,
             "reason": "not run: required package files are missing"}
            if missing else _native_host_doctor(installed))
    ok = (not missing and manifest_version == VERSION == module_version
          and server_assets and shipped["ok"] and imports.get("ok")
          and host.get("ok"))
    return {"ok": ok, "installed": str(installed), "version": manifest_version,
            "module_version": module_version, "missing": missing,
            "required_runners": server_assets,
            "entrypoint_import_registration": imports,
            "native_host_doctor": host,
            "scan_ok": shipped["ok"], "scan_problems": shipped["problems"],
            "identity": "hermes-proactive-secretary",
            "scope": "package-local validation plus canonical host doctor; not live activation"}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_export")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--output", required=True)
    doc = sub.add_parser("doctor")
    doc.add_argument("--installed", required=True)
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "build":
            result = build_package(Path(args.output))
        elif args.command == "doctor":
            result = doctor(Path(args.installed))
        else:
            result = scan(Path(args.root))
    except Exception as exc:  # noqa: BLE001 - deterministic CLI failure
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
