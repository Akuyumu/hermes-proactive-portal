"""Synthetic repair tests for the parent-run finalization harness.

These tests never touch a live profile, network, scheduler or GitHub: every
subprocess control command, host context and privacy probe is an injected fake,
and every path is a temp directory. They lock in the independent-audit repairs:

* cron pause/resume return codes and exact status readback;
* atomic rollback with an honest retained-new inventory;
* full before/after protected-state inventory (add/remove/change);
* exact-byte ``user_config.json`` restore on any later failure;
* mandatory gh privacy verification before and after a push;
* hash-linked acceptance artifacts instead of bare gate booleans;
* an explicit owner non-billed-credit assertion (a positive balance is not proof);
* strictly allowlisted, non-raw error/content sanitisation.

The harness is excluded from the release/export, so the suite skips cleanly in
an installed tree.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "finalization"
if HARNESS.is_dir():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(HARNESS))


def _load(name: str, filename: str):
    path = HARNESS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _completed(args, code=0, out="", err=""):
    return subprocess.CompletedProcess(args, code, out, err)


def _write_jobs(path: Path, jobs: list[dict]) -> None:
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def _read_jobs(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["jobs"]


@unittest.skipUnless(HARNESS.is_dir(), "parent-run harness is not shipped")
class FinalizationRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._umask = os.umask(0o022)
        cls.addClassCleanup(os.umask, cls._umask)
        cls.cut = _load("fx_repair_cutover", "apply_cutover.py")
        cls.pub = _load("fx_repair_publish", "publish_private.py")
        cls.bench = _load("fx_repair_benchmark", "native_mcp_benchmark.py")

    # ------------------------------------------------------------------ #
    # Finding 1: cron control return codes + exact status readback
    # ------------------------------------------------------------------ #
    def _pause_env(self, jobs):
        tmp = Path(tempfile.mkdtemp())
        jobs_path = tmp / "jobs.json"
        _write_jobs(jobs_path, jobs)
        return tmp, jobs_path

    def test_pause_control_failure_aborts_without_resume_claim(self):
        tmp, jobs_path = self._pause_env([
            {"id": "a", "name": "n1", "enabled": True},
            {"id": "b", "name": "n2", "enabled": True},
        ])
        calls = []

        def fake_run(args, *, label, log_dir, timeout=240):
            calls.append(args)
            if args[:2] == ["cron", "pause"]:
                return _completed(args, 1, "", "boom")
            return _completed(args)

        original = self.cut._run
        self.cut._run = fake_run
        try:
            with self.assertRaises(RuntimeError):
                self.cut._pause_owned_jobs(["a", "b"], jobs_path, tmp)
        finally:
            self.cut._run = original
        self.assertFalse(any(c[:2] == ["cron", "resume"] for c in calls))

    def test_pause_unknown_status_aborts(self):
        tmp, jobs_path = self._pause_env([
            {"id": "a", "name": "n1"},
            {"id": "b", "name": "n2", "enabled": True},
        ])
        with self.assertRaises(RuntimeError):
            self.cut._pause_owned_jobs(["a", "b"], jobs_path, tmp)

    def test_pause_readback_must_report_paused(self):
        tmp, jobs_path = self._pause_env([
            {"id": "a", "name": "n1", "enabled": True},
            {"id": "b", "name": "n2", "enabled": True},
        ])

        def fake_run(args, *, label, log_dir, timeout=240):
            return _completed(args)  # succeeds but does NOT flip enabled

        original = self.cut._run
        self.cut._run = fake_run
        try:
            with self.assertRaises(RuntimeError):
                self.cut._pause_owned_jobs(["a", "b"], jobs_path, tmp)
        finally:
            self.cut._run = original

    def test_already_paused_job_is_not_claimed_as_paused(self):
        tmp, jobs_path = self._pause_env([
            {"id": "a", "name": "n1", "enabled": True},
            {"id": "b", "name": "n2", "enabled": False},
        ])

        def fake_run(args, *, label, log_dir, timeout=240):
            if args[:2] == ["cron", "pause"]:
                jobs = _read_jobs(jobs_path)
                for job in jobs:
                    if str(job["id"]) == args[2]:
                        job["enabled"] = False
                _write_jobs(jobs_path, jobs)
            return _completed(args)

        original = self.cut._run
        self.cut._run = fake_run
        try:
            paused = self.cut._pause_owned_jobs(["a", "b"], jobs_path, tmp)
        finally:
            self.cut._run = original
        self.assertEqual(paused, ["a"])

    def test_resume_failure_is_not_reported_as_resumed(self):
        tmp, jobs_path = self._pause_env([{"id": "a", "name": "n1", "enabled": False}])

        def fake_run(args, *, label, log_dir, timeout=240):
            if args[:2] == ["cron", "resume"]:
                return _completed(args, 1, "", "nope")
            return _completed(args)

        original = self.cut._run
        self.cut._run = fake_run
        try:
            with self.assertRaises(RuntimeError):
                self.cut._resume_jobs(["a"], jobs_path, tmp)
        finally:
            self.cut._run = original

    def test_resume_success_verifies_readback(self):
        tmp, jobs_path = self._pause_env([{"id": "a", "name": "n1", "enabled": False}])

        def fake_run(args, *, label, log_dir, timeout=240):
            if args[:2] == ["cron", "resume"]:
                jobs = _read_jobs(jobs_path)
                for job in jobs:
                    if str(job["id"]) == args[2]:
                        job["enabled"] = True
                _write_jobs(jobs_path, jobs)
            return _completed(args)

        original = self.cut._run
        self.cut._run = fake_run
        try:
            self.assertEqual(self.cut._resume_jobs(["a"], jobs_path, tmp), ["a"])
        finally:
            self.cut._run = original

    def test_job_inventory_detects_metadata_and_set_changes(self):
        before = {"a": {"name": "n", "schedule": "* * * * *"}}
        self.cut._verify_job_inventory(before, {"a": {"name": "n", "schedule": "* * * * *"}})
        with self.assertRaises(RuntimeError):
            self.cut._verify_job_inventory(before, {"a": {"name": "n", "schedule": "0 0 * * *"}})
        with self.assertRaises(RuntimeError):
            self.cut._verify_job_inventory(before, {})

    # ------------------------------------------------------------------ #
    # Finding 3: full protected-state inventory (add/remove/change)
    # ------------------------------------------------------------------ #
    def test_state_inventory_blocks_unexplained_additions(self):
        before = {"a.json": "1"}
        after = {"a.json": "1", "surprise.json": "2"}
        with self.assertRaises(RuntimeError):
            self.cut._verify_state_inventory(before, after, {}, set())

    def test_state_inventory_allows_only_named_new_lock(self):
        before = {"a.json": "1"}
        after = {"a.json": "1", ".lock": ""}
        diff = self.cut._verify_state_inventory(before, after, {}, {".lock"})
        self.assertEqual(diff["added"], [".lock"])

    def test_state_inventory_blocks_removals(self):
        with self.assertRaises(RuntimeError):
            self.cut._verify_state_inventory({"a.json": "1"}, {}, {}, set())

    def test_state_inventory_blocks_unexplained_change_and_allows_expected(self):
        with self.assertRaises(RuntimeError):
            self.cut._verify_state_inventory({"a.json": "1"}, {"a.json": "2"}, {}, set())
        diff = self.cut._verify_state_inventory(
            {"user_config.json": "1"}, {"user_config.json": "2"},
            {"user_config.json": "2"}, set())
        self.assertEqual(diff["changed"], ["user_config.json"])

    # ------------------------------------------------------------------ #
    # Finding 2: atomic rollback restore
    # ------------------------------------------------------------------ #
    def test_atomic_restore_restores_exact_bytes_and_mode(self):
        tmp = Path(tempfile.mkdtemp())
        src = tmp / "backup" / "old.py"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"OLD\n")
        os.chmod(src, 0o640)
        dst = tmp / "live" / "old.py"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"NEW\n")
        self.cut._atomic_restore(src, dst)
        self.assertEqual(dst.read_bytes(), b"OLD\n")
        self.assertEqual(dst.stat().st_mode & 0o777, 0o640)

    # ------------------------------------------------------------------ #
    # Finding 4: explicit retrieval diff + exact-byte restore
    # ------------------------------------------------------------------ #
    def test_only_retrieval_changed_detects_unrelated_edits(self):
        original = {"retrieval": {"keep": 1}, "other": {"x": 1}}
        desired = {"firecrawl_enabled": True}
        good = {"retrieval": {"keep": 1, "firecrawl_enabled": True}, "other": {"x": 1}}
        self.assertTrue(self.cut._only_retrieval_changed(original, good, desired))
        bad_top = {"retrieval": {"keep": 1, "firecrawl_enabled": True}, "other": {"x": 2}}
        self.assertFalse(self.cut._only_retrieval_changed(original, bad_top, desired))
        bad_nested = {"retrieval": {"keep": 9, "firecrawl_enabled": True}, "other": {"x": 1}}
        self.assertFalse(self.cut._only_retrieval_changed(original, bad_nested, desired))

    def test_apply_retrieval_config_restores_on_validation_failure(self):
        tmp = Path(tempfile.mkdtemp())
        data = tmp / "data"
        data.mkdir()
        path = data / "user_config.json"
        original = b'{"retrieval": {"keep": true}, "other": {"x": 1}}\n'
        path.write_bytes(original)

        def fake_run(args, *, label, log_dir, timeout=240):
            return _completed(args, 1, "", "invalid")

        original_run = self.cut._run
        self.cut._run = fake_run
        try:
            with self.assertRaises(SystemExit):
                self.cut._apply_retrieval_config(data, server="s", tool="t", log_dir=tmp)
        finally:
            self.cut._run = original_run
        self.assertEqual(path.read_bytes(), original)

    # ------------------------------------------------------------------ #
    # Integration: rollback on a later failure stays honest
    # ------------------------------------------------------------------ #
    def _run_main_rollback(self, *, resume_flips: bool = True, fail_pause_job: str | None = None):
        tmp = Path(tempfile.mkdtemp())
        home = tmp / "home"
        data = home / "plugin-data" / self.cut.common.PLUGIN_ID
        live = home / "plugins" / self.cut.common.PLUGIN_ID
        cron_dir = home / "cron"
        final = tmp / "finalization"
        for directory in (data, live, cron_dir, final):
            directory.mkdir(parents=True, exist_ok=True)

        jobs = [
            {"id": "job-1", "name": "pilot", "schedule": "*/15 * * * *",
             "script": "pilot.py", "deliver": "local", "enabled": True},
            {"id": "job-2", "name": "reminders", "schedule": "* * * * *",
             "script": "reminders.py", "deliver": "local", "enabled": True},
        ]
        _write_jobs(cron_dir / "jobs.json", jobs)
        (data / "cron_job_ownership.json").write_text(json.dumps({"jobs": {
            "pilot": {"id": "job-1", "name": "pilot", "schedule": "*/15 * * * *",
                      "script": "pilot.py", "deliver": "local"},
            "reminders": {"id": "job-2", "name": "reminders", "schedule": "* * * * *",
                          "script": "reminders.py", "deliver": "local"},
        }}), encoding="utf-8")
        original_config = b'{"retrieval": {"keep": true}, "other": {"x": 1}}\n'
        (data / "user_config.json").write_bytes(original_config)
        (live / "old.py").write_bytes(b"OLD\n")

        release = final / self.cut.common.RELEASE_DIR_NAME
        release.mkdir(parents=True)
        (release / "old.py").write_bytes(b"NEW\n")
        (release / "new.py").write_bytes(b"BRAND NEW\n")
        manifest = {
            "old.py": self.cut.common.sha256_file(release / "old.py"),
            "new.py": self.cut.common.sha256_file(release / "new.py"),
        }
        manifest_path = final / self.cut.common.MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        def fake_run(args, *, label, log_dir, timeout=240):
            verb = args[:2]
            if verb == ["cron", "pause"]:
                current = _read_jobs(cron_dir / "jobs.json")
                for job in current:
                    if str(job["id"]) == args[2]:
                        job["enabled"] = False
                _write_jobs(cron_dir / "jobs.json", current)
                if fail_pause_job == args[2]:
                    # Mutated the job and THEN failed: the pause still happened.
                    return _completed(args, 1, "", "boom after mutate")
                return _completed(args)
            if verb == ["cron", "resume"]:
                if resume_flips:
                    current = _read_jobs(cron_dir / "jobs.json")
                    for job in current:
                        if str(job["id"]) == args[2]:
                            job["enabled"] = True
                    _write_jobs(cron_dir / "jobs.json", current)
                return _completed(args)  # exit 0 even when the status did not flip
            if "doctor" in args:
                return _completed(args, 1, "", "doctor failed")
            return _completed(args)

        original_final = self.cut.FINALIZATION
        original_home = self.cut._home
        original_run = self.cut._run
        original_acceptance = self.cut._require_acceptance
        original_suite = self.cut._require_suite
        self.cut.FINALIZATION = final
        self.cut._home = lambda: home
        self.cut._run = fake_run
        self.cut._require_acceptance = lambda sha: {}
        self.cut._require_suite = lambda acceptance: 904
        saved_argv = sys.argv
        sys.argv = ["apply_cutover.py", "--confirm", "--enable-firecrawl",
                    "--firecrawl-server", "s", "--firecrawl-tool", "t", "--schema-verified"]
        try:
            with self.assertRaises(RuntimeError):
                self.cut.main()
        finally:
            sys.argv = saved_argv
            self.cut.FINALIZATION = original_final
            self.cut._home = original_home
            self.cut._run = original_run
            self.cut._require_acceptance = original_acceptance
            self.cut._require_suite = original_suite
        return live, data, cron_dir, final, original_config

    def test_main_rollback_restores_config_and_retains_new_files(self):
        live, data, cron_dir, final, original_config = self._run_main_rollback()

        self.assertEqual((live / "old.py").read_bytes(), b"OLD\n")
        self.assertTrue((live / "new.py").is_file())
        self.assertEqual((data / "user_config.json").read_bytes(), original_config)
        rollback = json.loads((final / "cutover-rollback.json").read_text(encoding="utf-8"))
        self.assertTrue(rollback["attempted"])
        self.assertIn("new.py", rollback["retained_new"])
        self.assertTrue(rollback["incomplete"])
        self.assertTrue(rollback["user_config_restored"])
        resumed = {job["id"]: job["enabled"] for job in _read_jobs(cron_dir / "jobs.json")}
        self.assertEqual(resumed, {"job-1": True, "job-2": True})

    def test_rollback_resume_exit_ok_but_status_still_paused_is_not_claimed(self):
        # Finding 1: a rollback resume that exits 0 but does not actually re-enable
        # the job must NOT be recorded as resumed.
        live, data, cron_dir, final, _original_config = self._run_main_rollback(resume_flips=False)
        rollback = json.loads((final / "cutover-rollback.json").read_text(encoding="utf-8"))
        self.assertEqual(rollback["resumed"], [])
        self.assertTrue(rollback["incomplete"])
        self.assertTrue(any("readback not resumed" in e for e in rollback["errors"]))
        statuses = {job["id"]: job["enabled"] for job in _read_jobs(cron_dir / "jobs.json")}
        self.assertEqual(statuses, {"job-1": False, "job-2": False})

    # ------------------------------------------------------------------ #
    # Finding 6: acceptance artifacts must be linked, not just booleans
    # ------------------------------------------------------------------ #
    def _acceptance_fixture(self, tmp: Path):
        final = tmp / "finalization"
        final.mkdir(parents=True, exist_ok=True)
        bench = final / "native-mcp-benchmark.json"
        bench.write_text(json.dumps({
            "ok": True, "classification": "rendered_markdown",
            "native_context": True, "grant_ok": True,
        }), encoding="utf-8")
        no_send = final / "no-send.json"
        no_send.write_text(json.dumps({"ok": True, "notifications_sent": 0}), encoding="utf-8")
        suite = final / "parent-final-suite.log"
        suite.write_text("Ran 905 tests in 1.0s\n\nOK\n", encoding="utf-8")
        manifest = final / self.cut.common.MANIFEST_NAME
        manifest.write_text("{}", encoding="utf-8")
        manifest_sha = self.cut.common.sha256_file(manifest)
        gate = {
            "independent_review_passed": True, "luna_accepted": True,
            "real_no_send_passed": True, "benchmark_passed": True,
            "no_notifications": True, "parent_authority": "owner",
            "reviewer": "reviewer", "luna_review_id": "luna-1",
            "parent_review_ids": ["pr-1"],
            "benchmark_report": str(bench), "benchmark_report_sha256": self.cut.common.sha256_file(bench),
            "no_send_report": str(no_send), "no_send_report_sha256": self.cut.common.sha256_file(no_send),
            "suite_log": str(suite), "suite_log_sha256": self.cut.common.sha256_file(suite),
            "suite_exit_code": 0, "candidate_manifest_sha256": manifest_sha,
            "frozen_manifest_sha256": manifest_sha,
        }
        (final / "FINALIZATION_ACCEPTANCE.json").write_text(json.dumps(gate), encoding="utf-8")
        return final, manifest_sha

    def test_acceptance_requires_linked_artifacts(self):
        tmp = Path(tempfile.mkdtemp())
        final, manifest_sha = self._acceptance_fixture(tmp)
        original_final, original_root = self.cut.FINALIZATION, self.cut.ROOT
        self.cut.FINALIZATION, self.cut.ROOT = final, tmp
        os.environ["HERMES_CUTOVER_OWNER_ACK"] = "1"
        try:
            data = self.cut._require_acceptance(manifest_sha)
            self.assertTrue(data["luna_accepted"])
            # Tamper the benchmark report: the linked hash must reject it.
            (final / "native-mcp-benchmark.json").write_text(json.dumps({
                "ok": True, "classification": "blocked",
                "native_context": True, "grant_ok": True,
            }), encoding="utf-8")
            with self.assertRaises(SystemExit):
                self.cut._require_acceptance(manifest_sha)
        finally:
            self.cut.FINALIZATION, self.cut.ROOT = original_final, original_root
            os.environ.pop("HERMES_CUTOVER_OWNER_ACK", None)

    # ------------------------------------------------------------------ #
    # Finding 5: mandatory gh privacy before+after; stale abort
    # ------------------------------------------------------------------ #
    def test_privacy_aborts_when_gh_missing(self):
        original = self.pub.shutil
        self.pub.shutil = types.SimpleNamespace(which=lambda name: None)
        try:
            with self.assertRaises(SystemExit):
                self.pub._read_privacy("owner/repo", stage="pre-push")
        finally:
            self.pub.shutil = original

    def test_privacy_requires_private_true(self):
        original_shutil, original_run = self.pub.shutil, self.pub._run
        self.pub.shutil = types.SimpleNamespace(which=lambda name: "/usr/bin/gh")
        self.pub._run = lambda cmd, **kwargs: _completed(cmd, 0, '{"isPrivate": false, "nameWithOwner": "o/r"}')
        try:
            with self.assertRaises(SystemExit):
                self.pub._read_privacy("owner/repo", stage="pre-push")
        finally:
            self.pub.shutil, self.pub._run = original_shutil, original_run

    def test_privacy_error_aborts(self):
        original_shutil, original_run = self.pub.shutil, self.pub._run
        self.pub.shutil = types.SimpleNamespace(which=lambda name: "/usr/bin/gh")
        self.pub._run = lambda cmd, **kwargs: _completed(cmd, 1, "", "boom")
        try:
            with self.assertRaises(SystemExit):
                self.pub._read_privacy("owner/repo", stage="post-push")
        finally:
            self.pub.shutil, self.pub._run = original_shutil, original_run

    def test_cutover_artifact_precondition(self):
        tmp = Path(tempfile.mkdtemp())
        original_final = self.pub.FINALIZATION
        self.pub.FINALIZATION = tmp
        try:
            with self.assertRaises(SystemExit):
                self.pub._require_cutover_artifact("abc")
            (tmp / "cutover-applied.json").write_text(json.dumps({
                "ok": True, "installed_manifest_sha256": "abc"}), encoding="utf-8")
            self.assertTrue(self.pub._require_cutover_artifact("abc")["ok"])
            with self.assertRaises(SystemExit):
                self.pub._require_cutover_artifact("different")
        finally:
            self.pub.FINALIZATION = original_final

    # ------------------------------------------------------------------ #
    # Findings 7+8: owner credit evidence + strict sanitisation
    # ------------------------------------------------------------------ #
    def test_credit_evidence_requires_explicit_owner_assertion(self):
        self.assertFalse(self.bench.load_credit_evidence(None)["ok"])
        tmp = Path(tempfile.mkdtemp())
        partial = tmp / "partial.json"
        partial.write_text(json.dumps({"included_credits": True}), encoding="utf-8")
        self.assertFalse(self.bench.load_credit_evidence(str(partial))["ok"])
        good = tmp / "good.json"
        good.write_text(json.dumps({
            "included_credits": True, "no_autobilling": True,
            "owner": "owner", "review_id": "r-1"}), encoding="utf-8")
        self.assertTrue(self.bench.load_credit_evidence(str(good))["ok"])

    def test_generic_codes_never_echo_raw_text(self):
        synthetic_key = "sk-" + "abcdefghijklmnopqrstuv"
        code = self.bench._classify_reason("HTTP 429 token=SECRET123 " + synthetic_key)
        self.assertEqual(code, "quota_deferred")
        self.assertNotIn("SECRET", code)
        self.assertIn(self.bench._classify_reason("boom"), self.bench._GENERIC_CODES)

    def test_safe_excerpt_strips_secrets_and_url_query(self):
        synthetic_key = "sk-" + "abcdefghijklmnopqrstuvwx"
        raw = ("Public body https://example.com/p?token=SECRET1234567890abcdef "
               "and key " + synthetic_key + " plus more text")
        out = self.bench._safe_excerpt(raw)
        self.assertNotIn("SECRET1234567890abcdef", out)
        self.assertNotIn(synthetic_key, out)
        self.assertIn("?[redacted]", out)

    def test_credit_fields_are_booleans_only(self):
        fields = self.bench._extract_credit_fields(
            json.dumps({"remainingCredits": 42, "planCredits": 100, "accountId": "acct-9"}))
        self.assertEqual(set(fields), {"remaining_present", "remaining_positive",
                                       "plan_present", "period_present"})
        self.assertTrue(all(isinstance(v, bool) for v in fields.values()))

    def _bench_args(self, tmp: Path, evidence: str | None, confirm: bool):
        return types.SimpleNamespace(
            contract=str(ROOT / "host-references" / "firecrawl-contract.json"),
            tool="firecrawl_scrape", credit_tool="firecrawl_credit_usage",
            server="firecrawl", url="https://example.com/page", timeout=30.0,
            max_age=0, teststate_dir=str(tmp / "teststate-benchmark"),
            context_source=None, owner_included_credits_evidence=evidence,
            confirm_no_paid_charge=confirm, preflight_only=False,
        )

    def test_benchmark_blocks_positive_balance_without_owner_evidence(self):
        tmp = Path(tempfile.mkdtemp())

        class FakeCtx:
            def call_mcp(self, server, tool, arguments, timeout):
                return {"ok": True, "result": {"markdown": "# public body"}}

        original = {
            "grant": self.bench.grant_allows_server, "ctx": self.bench.resolve_context,
            "credit": self.bench.read_credit_usage, "root": self.bench.ROOT,
            "final": self.bench.FINALIZATION,
        }
        self.bench.grant_allows_server = lambda server: True
        self.bench.resolve_context = lambda source: FakeCtx()
        self.bench.read_credit_usage = lambda ctx, server, tool: {
            "ok": True, "fields": {"remaining_positive": True}, "envelope_sha256": "x"}
        self.bench.ROOT = tmp
        self.bench.FINALIZATION = tmp / "finalization"
        (tmp / "finalization").mkdir(parents=True, exist_ok=True)
        try:
            rc = self.bench.benchmark(self._bench_args(tmp, None, True))
            self.assertEqual(rc, 5)
        finally:
            self.bench.grant_allows_server = original["grant"]
            self.bench.resolve_context = original["ctx"]
            self.bench.read_credit_usage = original["credit"]
            self.bench.ROOT = original["root"]
            self.bench.FINALIZATION = original["final"]

    def test_benchmark_runs_with_owner_evidence_and_sanitizes_errors(self):
        tmp = Path(tempfile.mkdtemp())
        evidence = tmp / "evidence.json"
        evidence.write_text(json.dumps({
            "included_credits": True, "no_autobilling": True,
            "owner": "owner", "review_id": "r-1"}), encoding="utf-8")
        secret = "sk-" + "abcdefghijklmnopqrstuvwx"

        class ErrorCtx:
            def call_mcp(self, server, tool, arguments, timeout):
                return {"ok": False, "error": f"boom {secret} https://x.example/p?token=SECRET123"}

        original = {
            "grant": self.bench.grant_allows_server, "ctx": self.bench.resolve_context,
            "credit": self.bench.read_credit_usage, "root": self.bench.ROOT,
            "final": self.bench.FINALIZATION,
        }
        self.bench.grant_allows_server = lambda server: True
        self.bench.resolve_context = lambda source: ErrorCtx()
        self.bench.read_credit_usage = lambda ctx, server, tool: {
            "ok": True, "fields": {"remaining_positive": True}, "envelope_sha256": "x"}
        self.bench.ROOT = tmp
        self.bench.FINALIZATION = tmp / "finalization"
        (tmp / "finalization").mkdir(parents=True, exist_ok=True)
        try:
            rc = self.bench.benchmark(self._bench_args(tmp, str(evidence), True))
            self.assertEqual(rc, 6)
            written = (tmp / "finalization" / "native-mcp-benchmark.json").read_text(encoding="utf-8")
            self.assertNotIn(secret, written)
            self.assertNotIn("SECRET123", written)
            payload = json.loads(written)
            self.assertIn(payload["reason_code"], self.bench._GENERIC_CODES)
        finally:
            self.bench.grant_allows_server = original["grant"]
            self.bench.resolve_context = original["ctx"]
            self.bench.read_credit_usage = original["credit"]
            self.bench.ROOT = original["root"]
            self.bench.FINALIZATION = original["final"]

    def test_benchmark_refuses_without_no_paid_assertion(self):
        tmp = Path(tempfile.mkdtemp())
        evidence = tmp / "evidence.json"
        evidence.write_text(json.dumps({
            "included_credits": True, "no_autobilling": True,
            "owner": "owner", "review_id": "r-1"}), encoding="utf-8")

        class FakeCtx:
            def call_mcp(self, server, tool, arguments, timeout):
                return {"ok": True, "result": {"markdown": "# body"}}

        original = {
            "grant": self.bench.grant_allows_server, "ctx": self.bench.resolve_context,
            "credit": self.bench.read_credit_usage, "root": self.bench.ROOT,
            "final": self.bench.FINALIZATION,
        }
        self.bench.grant_allows_server = lambda server: True
        self.bench.resolve_context = lambda source: FakeCtx()
        self.bench.read_credit_usage = lambda ctx, server, tool: {
            "ok": True, "fields": {"remaining_positive": True}, "envelope_sha256": "x"}
        self.bench.ROOT = tmp
        self.bench.FINALIZATION = tmp / "finalization"
        (tmp / "finalization").mkdir(parents=True, exist_ok=True)
        try:
            rc = self.bench.benchmark(self._bench_args(tmp, str(evidence), False))
            self.assertEqual(rc, 5)
        finally:
            self.bench.grant_allows_server = original["grant"]
            self.bench.resolve_context = original["ctx"]
            self.bench.read_credit_usage = original["credit"]
            self.bench.ROOT = original["root"]
            self.bench.FINALIZATION = original["final"]


    # ------------------------------------------------------------------ #
    # Residual findings: partial pause, retrieval timeout, symlink drift,
    # linked-suite/known-parent-run, path escape, post-push honesty.
    # ------------------------------------------------------------------ #
    def test_partial_pause_second_fails_preserves_first_in_accumulator(self):
        tmp, jobs_path = self._pause_env([
            {"id": "a", "name": "n1", "enabled": True},
            {"id": "b", "name": "n2", "enabled": True},
        ])

        def fake_run(args, *, label, log_dir, timeout=240):
            if args[:2] == ["cron", "pause"]:
                if args[2] == "a":
                    jobs = _read_jobs(jobs_path)
                    for job in jobs:
                        if str(job["id"]) == "a":
                            job["enabled"] = False
                    _write_jobs(jobs_path, jobs)
                    return _completed(args)
                return _completed(args, 1, "", "boom")  # second pause fails
            return _completed(args)

        original = self.cut._run
        self.cut._run = fake_run
        accumulator: list[str] = []
        try:
            with self.assertRaises(RuntimeError):
                self.cut._pause_owned_jobs(["a", "b"], jobs_path, tmp, accumulator)
        finally:
            self.cut._run = original
        # The successful first pause is preserved for the outer rollback.
        self.assertEqual(accumulator, ["a"])

    def test_pause_mutates_then_nonzero_is_reconciled_for_rollback(self):
        # A pause command can mutate the job and THEN exit non-zero. The exit
        # code is not proof the job was untouched, so the attempted job must be
        # read back and recorded as paused for the rollback.
        tmp, jobs_path = self._pause_env([
            {"id": "a", "name": "n1", "enabled": True},
            {"id": "b", "name": "n2", "enabled": True},
        ])

        def fake_run(args, *, label, log_dir, timeout=240):
            if args[:2] == ["cron", "pause"]:
                jobs = _read_jobs(jobs_path)
                for job in jobs:
                    if str(job["id"]) == args[2]:
                        job["enabled"] = False
                _write_jobs(jobs_path, jobs)
                return _completed(args, 1, "", "boom after mutate")
            return _completed(args)

        original = self.cut._run
        self.cut._run = fake_run
        paused: list[str] = []
        try:
            with self.assertRaises(self.cut.PauseAbort):
                self.cut._pause_owned_jobs(["a", "b"], jobs_path, tmp, paused)
        finally:
            self.cut._run = original
        # "a" really was paused before the control failed: it is not forgotten.
        self.assertEqual(paused, ["a"])
        statuses = {job["id"]: job["enabled"] for job in _read_jobs(jobs_path)}
        self.assertEqual(statuses, {"a": False, "b": True})

    def test_pause_mutates_then_timeout_is_reconciled_for_rollback(self):
        tmp, jobs_path = self._pause_env([{"id": "a", "name": "n1", "enabled": True}])

        def fake_run(args, *, label, log_dir, timeout=240):
            if args[:2] == ["cron", "pause"]:
                jobs = _read_jobs(jobs_path)
                for job in jobs:
                    if str(job["id"]) == args[2]:
                        job["enabled"] = False
                _write_jobs(jobs_path, jobs)
                raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
            return _completed(args)

        original = self.cut._run
        self.cut._run = fake_run
        paused: list[str] = []
        try:
            with self.assertRaises(self.cut.PauseAbort):
                self.cut._pause_owned_jobs(["a"], jobs_path, tmp, paused)
        finally:
            self.cut._run = original
        self.assertEqual(paused, ["a"])
        self.assertFalse(_read_jobs(jobs_path)[0]["enabled"])

    def test_pause_failure_with_unreadable_status_marks_incomplete(self):
        # If the attempted job's post-failure status cannot be determined, the
        # rollback must be marked incomplete and the job never silently dropped.
        tmp, jobs_path = self._pause_env([{"id": "a", "name": "n1", "enabled": True}])

        def fake_run(args, *, label, log_dir, timeout=240):
            if args[:2] == ["cron", "pause"]:
                jobs = _read_jobs(jobs_path)
                for job in jobs:
                    if str(job["id"]) == "a":
                        del job["enabled"]  # unknown state
                _write_jobs(jobs_path, jobs)
                return _completed(args, 1, "", "boom")
            return _completed(args)

        original = self.cut._run
        self.cut._run = fake_run
        paused: list[str] = []
        try:
            with self.assertRaises(self.cut.PauseAbort) as ctx:
                self.cut._pause_owned_jobs(["a"], jobs_path, tmp, paused)
        finally:
            self.cut._run = original
        self.assertTrue(ctx.exception.incomplete)
        self.assertEqual(paused, [])

    def test_main_rollback_resumes_attempted_then_failed_pause(self):
        # Integration: a pause that mutates then fails must be rolled back (the
        # job reports enabled again) and recorded as resumed.
        live, data, cron_dir, final, _config = self._run_main_rollback(
            fail_pause_job="job-2")
        statuses = {job["id"]: job["enabled"] for job in _read_jobs(cron_dir / "jobs.json")}
        self.assertEqual(statuses, {"job-1": True, "job-2": True})
        rollback = json.loads((final / "cutover-rollback.json").read_text(encoding="utf-8"))
        self.assertIn("job-1", rollback["resumed"])
        self.assertIn("job-2", rollback["resumed"])
        self.assertFalse(rollback["incomplete"])

    def test_apply_retrieval_config_restores_exact_bytes_on_timeout_after_write(self):
        tmp = Path(tempfile.mkdtemp())
        data = tmp / "data"
        data.mkdir()
        path = data / "user_config.json"
        original = b'{"retrieval": {"keep": true}, "other": {"x": 1}}\n'
        path.write_bytes(original)

        def fake_run(args, *, label, log_dir, timeout=240):
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

        original_run = self.cut._run
        self.cut._run = fake_run
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.cut._apply_retrieval_config(data, server="s", tool="t", log_dir=tmp)
        finally:
            self.cut._run = original_run
        self.assertEqual(path.read_bytes(), original)

    def test_state_inventory_records_symlinks_without_following(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "a.json").write_text("{}", encoding="utf-8")
        before = self.cut._hash_tree(tmp)
        (tmp / "link").symlink_to("a.json")
        after = self.cut._hash_tree(tmp)
        self.assertTrue(after["link"].startswith("symlink:"))
        with self.assertRaises(RuntimeError):
            self.cut._verify_state_inventory(before, after, {}, set())

    def test_state_inventory_detects_regular_file_replaced_by_symlink(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "c").write_text("real", encoding="utf-8")
        before = self.cut._hash_tree(tmp)
        (tmp / "c").unlink()
        (tmp / "c").symlink_to("elsewhere")
        after = self.cut._hash_tree(tmp)
        with self.assertRaises(RuntimeError):
            self.cut._verify_state_inventory(before, after, {}, set())

    def test_unexplained_ledger_creation_is_rejected_without_verified_operation(self):
        tmp = Path(tempfile.mkdtemp())
        before = {"user_config.json": "x"}
        after = {"user_config.json": "x", "metered_retrieval.json": "sha"}
        allowed = self.cut._benign_retrieval_new_state(before, after, tmp, enabled=False)
        self.assertEqual(allowed, set())
        with self.assertRaises(RuntimeError):
            self.cut._verify_state_inventory(
                before, after, {}, set(self.cut.ALLOWED_NEW_STATE) | allowed)

    def test_benign_ledger_requires_wellformed_content(self):
        tmp = Path(tempfile.mkdtemp())
        before = {"user_config.json": "x"}
        after = {"user_config.json": "x", "metered_retrieval.json": "sha"}
        (tmp / "metered_retrieval.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(
            self.cut._benign_retrieval_new_state(before, after, tmp, enabled=True), set())
        (tmp / "metered_retrieval.json").write_text(json.dumps({
            "schema": 1, "day": "2026-10-06", "day_used": 1,
            "cooldown_until": "", "scraped": {}}), encoding="utf-8")
        allowed = self.cut._benign_retrieval_new_state(before, after, tmp, enabled=True)
        self.assertIn("metered_retrieval.json", allowed)
        diff = self.cut._verify_state_inventory(
            before, after, {}, set(self.cut.ALLOWED_NEW_STATE) | allowed)
        self.assertEqual(diff["added"], ["metered_retrieval.json"])

    def test_acceptance_ok_count_come_from_linked_artifact(self):
        tmp = Path(tempfile.mkdtemp())
        final, manifest_sha = self._acceptance_fixture(tmp)
        linked = final / "other-run.log"
        linked.write_text("Ran 200 tests in 1.0s\n\nFAILED (failures=1)\n", encoding="utf-8")
        gate_path = final / "FINALIZATION_ACCEPTANCE.json"
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        gate["suite_log"] = str(linked)
        gate["suite_log_sha256"] = self.cut.common.sha256_file(linked)
        gate_path.write_text(json.dumps(gate), encoding="utf-8")
        # A stale OK log at the old fixed path must NOT rescue the failed linked one.
        (final / "parent-final-suite.log").write_text(
            "Ran 999 tests in 1.0s\n\nOK\n", encoding="utf-8")
        original_final, original_root = self.cut.FINALIZATION, self.cut.ROOT
        self.cut.FINALIZATION, self.cut.ROOT = final, tmp
        os.environ["HERMES_CUTOVER_OWNER_ACK"] = "1"
        try:
            with self.assertRaises(SystemExit):
                self.cut._require_acceptance(manifest_sha)
        finally:
            self.cut.FINALIZATION, self.cut.ROOT = original_final, original_root
            os.environ.pop("HERMES_CUTOVER_OWNER_ACK", None)

    def test_acceptance_requires_candidate_manifest_linkage(self):
        tmp = Path(tempfile.mkdtemp())
        final, manifest_sha = self._acceptance_fixture(tmp)
        gate_path = final / "FINALIZATION_ACCEPTANCE.json"
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        del gate["candidate_manifest_sha256"]
        gate_path.write_text(json.dumps(gate), encoding="utf-8")
        original_final, original_root = self.cut.FINALIZATION, self.cut.ROOT
        self.cut.FINALIZATION, self.cut.ROOT = final, tmp
        os.environ["HERMES_CUTOVER_OWNER_ACK"] = "1"
        try:
            with self.assertRaises(SystemExit):
                self.cut._require_acceptance(manifest_sha)
        finally:
            self.cut.FINALIZATION, self.cut.ROOT = original_final, original_root
            os.environ.pop("HERMES_CUTOVER_OWNER_ACK", None)

    def test_workspace_path_rejects_prefix_sibling(self):
        base = Path(tempfile.mkdtemp())
        root = base / "candidate"
        root.mkdir()
        sibling = base / "candidate-evil"
        sibling.mkdir()
        (sibling / "artifact.json").write_text("{}", encoding="utf-8")
        (root / "ok").mkdir()
        (root / "ok" / "a.json").write_text("{}", encoding="utf-8")
        original_root = self.cut.ROOT
        self.cut.ROOT = root
        try:
            with self.assertRaises(SystemExit):
                self.cut._workspace_path(str(sibling / "artifact.json"))
            self.assertEqual(self.cut._workspace_path("ok/a.json"), (root / "ok" / "a.json").resolve())
        finally:
            self.cut.ROOT = original_root

    def test_workspace_path_rejects_symlink_escape(self):
        base = Path(tempfile.mkdtemp())
        root = base / "candidate"
        root.mkdir()
        outside = base / "outside"
        outside.mkdir()
        (outside / "secret.json").write_text("{}", encoding="utf-8")
        (root / "escape").symlink_to(outside, target_is_directory=True)
        original_root = self.cut.ROOT
        self.cut.ROOT = root
        try:
            with self.assertRaises(SystemExit):
                self.cut._workspace_path("escape/secret.json")
        finally:
            self.cut.ROOT = original_root

    def test_post_push_privacy_failure_writes_honest_partial_record(self):
        tmp = Path(tempfile.mkdtemp())
        final = tmp / "finalization"
        final.mkdir()
        release = final / self.pub.common.RELEASE_DIR_NAME
        release.mkdir()
        (release / "mod.py").write_text("x = 1\n", encoding="utf-8")
        manifest = {"mod.py": self.pub.common.sha256_file(release / "mod.py")}
        manifest_path = final / self.pub.common.MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_sha = self.pub.common.sha256_file(manifest_path)
        (final / "cutover-applied.json").write_text(json.dumps({
            "ok": True, "installed_manifest_sha256": manifest_sha}), encoding="utf-8")
        workdir = tmp / "checkout"
        (workdir / ".git").mkdir(parents=True)
        local_head = "a" * 40

        def fake_run(cmd, *, cwd=None, timeout=300):
            return _completed(cmd, 0, "", "")

        def fake_git_ok(workdir_arg, *args):
            if args[:1] == ("status",):
                return ""
            if args[:1] == ("ls-files",):
                return "mod.py\n"
            if args[:1] == ("diff",):
                return "M\tmod.py\n"
            if args[:1] == ("rev-parse",):
                return local_head + "\n"
            return ""

        def fake_privacy(remote, *, stage):
            if stage == "post-push":
                raise SystemExit("refusal: repository is not verified private (post-push)")
            return {"checked": True, "stage": stage, "is_private": True, "visibility_changed": False}

        original = {
            "root": self.pub.ROOT, "final": self.pub.FINALIZATION,
            "run": self.pub._run, "git_ok": self.pub._git_ok,
            "privacy": self.pub._read_privacy,
        }
        self.pub.ROOT = tmp
        self.pub.FINALIZATION = final
        self.pub._run = fake_run
        self.pub._git_ok = fake_git_ok
        self.pub._read_privacy = fake_privacy
        saved_argv = sys.argv
        sys.argv = ["publish_private.py", "--confirm", "--workdir", str(workdir)]
        try:
            with self.assertRaises(SystemExit):
                self.pub.main()
        finally:
            sys.argv = saved_argv
            self.pub.ROOT = original["root"]
            self.pub.FINALIZATION = original["final"]
            self.pub._run = original["run"]
            self.pub._git_ok = original["git_ok"]
            self.pub._read_privacy = original["privacy"]

        partial = json.loads((final / "private-publication.json").read_text(encoding="utf-8"))
        self.assertFalse(partial["ok"])
        self.assertTrue(partial["partial"])
        self.assertTrue(partial["push_occurred"])
        self.assertTrue(partial["verification_failed"])
        self.assertEqual(partial["commit"], local_head)
        self.assertEqual(partial["remote_sha"], local_head)

    # ------------------------------------------------------------------ #
    # Residual finding: post-push boundary + ambiguous failed-push outcome.
    # ------------------------------------------------------------------ #
    def _publish_fixture(self):
        tmp = Path(tempfile.mkdtemp())
        final = tmp / "finalization"
        final.mkdir()
        release = final / self.pub.common.RELEASE_DIR_NAME
        release.mkdir()
        (release / "mod.py").write_text("x = 1\n", encoding="utf-8")
        manifest = {"mod.py": self.pub.common.sha256_file(release / "mod.py")}
        manifest_path = final / self.pub.common.MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_sha = self.pub.common.sha256_file(manifest_path)
        (final / "cutover-applied.json").write_text(json.dumps({
            "ok": True, "installed_manifest_sha256": manifest_sha}), encoding="utf-8")
        workdir = tmp / "checkout"
        (workdir / ".git").mkdir(parents=True)
        return tmp, final, workdir, manifest_sha, "a" * 40

    def _invoke_publish(self, *, tmp, final, workdir, local_head, push_rc=0,
                        fetch_raises=False, rev_parse=None, post_privacy_ok=True,
                        cutover=None, write_json=None):
        rev_parse = local_head + "\n" if rev_parse is None else rev_parse

        def fake_run(cmd, *, cwd=None, timeout=300):
            if "push" in cmd:
                return _completed(cmd, push_rc, "", "boom" if push_rc else "")
            return _completed(cmd, 0, "", "")

        def fake_git_ok(workdir_arg, *args):
            if args[:1] == ("status",):
                return ""
            if args[:1] == ("ls-files",):
                return "mod.py\n"
            if args[:1] == ("diff",):
                return "M\tmod.py\n"
            if args[:1] == ("fetch",):
                if fetch_raises:
                    raise SystemExit("git fetch failed")
                return ""
            if args[:1] == ("rev-parse",):
                if args[1:2] == ("HEAD",):
                    return local_head + "\n"
                return rev_parse
            return ""

        def fake_privacy(remote, *, stage):
            if stage == "post-push" and not post_privacy_ok:
                raise SystemExit("refusal: repository is not verified private (post-push)")
            return {"checked": True, "stage": stage, "is_private": True,
                    "visibility_changed": False}

        originals = {
            "root": self.pub.ROOT, "final": self.pub.FINALIZATION,
            "run": self.pub._run, "git_ok": self.pub._git_ok,
            "privacy": self.pub._read_privacy,
            "cutover": self.pub._require_cutover_artifact,
            "write": self.pub.common.write_json,
        }
        self.pub.ROOT = tmp
        self.pub.FINALIZATION = final
        self.pub._run = fake_run
        self.pub._git_ok = fake_git_ok
        self.pub._read_privacy = fake_privacy
        if cutover is not None:
            self.pub._require_cutover_artifact = cutover
        if write_json is not None:
            self.pub.common.write_json = write_json
        saved_argv = sys.argv
        sys.argv = ["publish_private.py", "--confirm", "--workdir", str(workdir)]
        raised = None
        try:
            self.pub.main()
        except BaseException as exc:  # noqa: BLE001 - the failure IS the subject
            raised = exc
        finally:
            sys.argv = saved_argv
            self.pub.ROOT = originals["root"]
            self.pub.FINALIZATION = originals["final"]
            self.pub._run = originals["run"]
            self.pub._git_ok = originals["git_ok"]
            self.pub._read_privacy = originals["privacy"]
            self.pub._require_cutover_artifact = originals["cutover"]
            self.pub.common.write_json = originals["write"]
        return raised

    def test_post_push_late_cutover_artifact_failure_records_push(self):
        # The SECOND cutover-artifact read happens after the push; a failure
        # there must still record push_occurred=true.
        tmp, final, workdir, _manifest_sha, head = self._publish_fixture()
        calls = {"n": 0}

        def flaky_cutover(sha):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise SystemExit("refusal: cutover installed manifest does not match")
            return {"ok": True, "installed_manifest_sha256": sha}

        raised = self._invoke_publish(tmp=tmp, final=final, workdir=workdir,
                                      local_head=head, cutover=flaky_cutover)
        self.assertIsInstance(raised, SystemExit)
        record = json.loads((final / "private-publication.json").read_text(encoding="utf-8"))
        self.assertFalse(record["ok"])
        self.assertTrue(record["partial"])
        self.assertTrue(record["push_occurred"])
        self.assertEqual(record["push_outcome"], "verified_occurred")
        self.assertEqual(record["commit"], head)

    def test_post_push_success_record_write_failure_records_push(self):
        tmp, final, workdir, _manifest_sha, head = self._publish_fixture()
        real_write = self.pub.common.write_json

        def flaky_write(path, payload, mode=0o600):
            if isinstance(payload, dict) and payload.get("partial"):
                return real_write(path, payload, mode)
            raise OSError("disk full writing success record")

        raised = self._invoke_publish(tmp=tmp, final=final, workdir=workdir,
                                      local_head=head, write_json=flaky_write)
        self.assertIsInstance(raised, OSError)
        record = json.loads((final / "private-publication.json").read_text(encoding="utf-8"))
        self.assertTrue(record["push_occurred"])
        self.assertEqual(record["commit"], head)

    def test_post_push_unwritable_record_discloses_remote_side_effect(self):
        tmp, final, workdir, _manifest_sha, head = self._publish_fixture()

        def always_fail(path, payload, mode=0o600):
            raise OSError("disk full")

        raised = self._invoke_publish(tmp=tmp, final=final, workdir=workdir,
                                      local_head=head, write_json=always_fail)
        self.assertIsInstance(raised, RuntimeError)
        self.assertIn("side effect", str(raised))
        self.assertFalse((final / "private-publication.json").exists())

    def test_failed_push_matching_remote_sha_is_recorded_as_occurred(self):
        tmp, final, workdir, _manifest_sha, head = self._publish_fixture()
        raised = self._invoke_publish(tmp=tmp, final=final, workdir=workdir,
                                      local_head=head, push_rc=1, rev_parse=head + "\n")
        self.assertIsInstance(raised, SystemExit)
        record = json.loads((final / "private-publication.json").read_text(encoding="utf-8"))
        self.assertTrue(record["push_occurred"])
        self.assertEqual(record["push_outcome"], "verified_occurred")
        self.assertEqual(record["push_exit_code"], 1)

    def test_failed_push_unreadable_remote_is_unknown_not_false(self):
        tmp, final, workdir, _manifest_sha, head = self._publish_fixture()
        raised = self._invoke_publish(tmp=tmp, final=final, workdir=workdir,
                                      local_head=head, push_rc=1, fetch_raises=True)
        self.assertIsInstance(raised, SystemExit)
        record = json.loads((final / "private-publication.json").read_text(encoding="utf-8"))
        self.assertIsNone(record["push_occurred"])
        self.assertEqual(record["push_outcome"], "unknown")
        self.assertTrue(record["verification_failed"])

    def test_failed_push_different_remote_sha_is_verified_not_occurred(self):
        tmp, final, workdir, _manifest_sha, head = self._publish_fixture()
        raised = self._invoke_publish(tmp=tmp, final=final, workdir=workdir,
                                      local_head=head, push_rc=1, rev_parse="b" * 40 + "\n")
        self.assertIsInstance(raised, SystemExit)
        record = json.loads((final / "private-publication.json").read_text(encoding="utf-8"))
        self.assertFalse(record["push_occurred"])
        self.assertEqual(record["push_outcome"], "verified_not_occurred")


if __name__ == "__main__":
    unittest.main()
