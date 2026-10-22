"""Consumer gates reject incomplete or misbound Core evidence; no network calls."""
from __future__ import annotations

import copy
import hashlib
import io
import unittest
import zipfile

from release_evidence import (
    REQUIRED_JOBS,
    SCENARIOS,
    artifact_metadata,
    choose_source_run,
    validate_core_receipts,
    validate_matrix_archive,
    validate_source_jobs,
)


class CoreEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.artifact = {"commit": "a" * 40, "files": {"custom_components/uplus_reborn/__init__.py": "0" * 64}}
        self.rows = {name: {
            "schema": 1, "scenario": name, "success": True, "temporary_config_removed": True,
            "artifact": copy.deepcopy(self.artifact), "core_version": "2026.2.3",
            "installed_hashes_before": dict(self.artifact["files"]),
            "installed_hashes_after": dict(self.artifact["files"]), "shutdown": {"core_stopped": True},
        } for name in SCENARIOS}
        self.rows["profiles"]["restart"] = {"core_version": "2026.2.3", "shutdown": {"core_stopped": True}}

    def test_missing_scenario_cannot_be_called_a_complete_matrix(self):
        self.rows.pop("discovery")
        with self.assertRaisesRegex(ValueError, "scenario"):
            validate_core_receipts(self.rows, self.artifact, "2026.2.3")

    def test_success_for_another_commit_is_not_equivalent_package_evidence(self):
        self.rows["execution"]["artifact"]["commit"] = "b" * 40
        with self.assertRaises(ValueError):
            validate_core_receipts(self.rows, self.artifact, "2026.2.3")

    def test_wrong_core_version_and_failed_runtime_cannot_satisfy_the_target(self):
        self.rows["standby"]["core_version"] = "2026.10.2"
        with self.assertRaises(ValueError):
            validate_core_receipts(self.rows, self.artifact, "2026.2.3")
        self.rows["standby"]["core_version"] = "2026.2.3"
        self.rows["execution"]["success"] = False
        with self.assertRaises(ValueError):
            validate_core_receipts(self.rows, self.artifact, "2026.2.3")

    def test_missing_restart_or_cleanup_is_not_completed_acceptance(self):
        self.rows["profiles"].pop("restart")
        with self.assertRaises(ValueError):
            validate_core_receipts(self.rows, self.artifact, "2026.2.3")
        self.rows["profiles"]["restart"] = {"core_version": "2026.2.3", "shutdown": {"core_stopped": True}}
        self.rows["standby"]["temporary_config_removed"] = False
        with self.assertRaises(ValueError):
            validate_core_receipts(self.rows, self.artifact, "2026.2.3")


class BackendArtifactTests(unittest.TestCase):
    def test_duplicate_expired_or_foreign_artifacts_are_not_eligible(self):
        context = {"run_id": 12, "repository_id": 34, "head_repository_id": 34,
                   "head_sha": "a" * 40, "head_branch": "master"}
        artifact = {"id": 56, "name": "candidate", "digest": "sha256:" + "0" * 64,
                    "expired": False, "size_in_bytes": 100, "created_at": "2026-10-22T12:00:00Z",
                    "expires_at": "2026-11-05T12:00:00Z", "workflow_run": {
                        "id": 12, "repository_id": 34, "head_repository_id": 34,
                        "head_sha": "a" * 40, "head_branch": "master"}}
        self.assertEqual(artifact_metadata([artifact], "candidate", context)["id"], 56)
        for field, value in (("expired", True), ("digest", ""), ("digest", None), ("size_in_bytes", 0)):
            wrong = copy.deepcopy(artifact)
            wrong[field] = value
            with self.subTest(field=field), self.assertRaises((TypeError, ValueError)):
                artifact_metadata([wrong], "candidate", context)
        for field, value in (("id", 13), ("repository_id", 35), ("head_repository_id", 35),
                             ("head_sha", "b" * 40), ("head_branch", "foreign")):
            wrong = copy.deepcopy(artifact)
            wrong["workflow_run"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                artifact_metadata([wrong], "candidate", context)
        with self.assertRaises(ValueError):
            artifact_metadata([artifact, artifact], "candidate", context)


class TrustedSourceTests(unittest.TestCase):
    def setUp(self):
        self.repo = {"id": 34, "full_name": "Zuz666/uplus-reborn", "default_branch": "master", "private": False}
        self.workflow = {"id": 78, "path": ".github/workflows/ci.yml", "state": "active"}
        self.metadata = {"commit": "a" * 40}
        self.run = {"id": 12, "run_attempt": 1, "workflow_id": 78, "path": ".github/workflows/ci.yml",
                    "event": "push", "head_branch": "master", "head_sha": "a" * 40,
                    "repository": copy.deepcopy(self.repo), "head_repository": copy.deepcopy(self.repo),
                    "status": "completed", "conclusion": "success", "created_at": "2026-10-22T12:00:00Z"}
        self.context = {"run_id": 12, "attempt": 1, "repository": self.repo["full_name"]}
        self.jobs = [{"id": index, "run_id": 12, "run_attempt": 1, "name": name,
                      "status": "completed", "conclusion": "success",
                      "check_run_url": f"https://api.github.com/repos/Zuz666/uplus-reborn/check-runs/{index}",
                      "started_at": "2026-10-22T12:00:00Z", "completed_at": "2026-10-22T12:05:00Z"}
                     for index, name in enumerate(sorted(REQUIRED_JOBS), 1)]

    def test_newer_failure_or_pending_attempt_cannot_hide_behind_old_success(self):
        self.assertEqual(choose_source_run(self.repo, self.workflow, self.metadata, [self.run])["id"], 12)
        for status, conclusion in (("completed", "failure"), ("in_progress", None), ("completed", "cancelled")):
            newer = {**self.run, "id": 13, "created_at": "2026-10-22T12:10:00Z",
                     "status": status, "conclusion": conclusion}
            with self.subTest(status=status, conclusion=conclusion), self.assertRaises(ValueError):
                choose_source_run(self.repo, self.workflow, self.metadata, [self.run, newer])
        rerun = {**self.run, "run_attempt": 2, "status": "in_progress", "conclusion": None}
        with self.assertRaises(ValueError):
            choose_source_run(self.repo, self.workflow, self.metadata, [rerun])

    def test_green_pr_fork_wrong_branch_sha_or_workflow_is_not_a_source(self):
        for changes in (
            {"event": "pull_request"}, {"head_branch": "feature"}, {"head_sha": "b" * 40},
            {"head_repository": {**self.repo, "id": 35}}, {"workflow_id": 79},
            {"path": ".github/workflows/release.yml"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                choose_source_run(self.repo, self.workflow, self.metadata, [{**self.run, **changes}])
        with self.assertRaises(ValueError):
            choose_source_run(self.repo, {**self.workflow, "path": ".github/workflows/unrelated.yml"},
                              self.metadata, [self.run])

    def test_every_source_job_and_its_check_must_belong_to_the_selected_attempt(self):
        validate_source_jobs(self.context, self.jobs)
        missing = [row for row in self.jobs if row["name"] != "delivery / Isolated Core (test)"]
        with self.assertRaises(ValueError):
            validate_source_jobs(self.context, missing)
        for changes in (
            {"conclusion": "failure"}, {"status": "queued"}, {"run_attempt": 2}, {"run_id": 13},
            {"check_run_url": "https://api.github.com/repos/other/repo/check-runs/1"},
        ):
            wrong = copy.deepcopy(self.jobs)
            wrong[0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_source_jobs(self.context, wrong)


class AuthenticatedMatrixBytesTests(unittest.TestCase):
    def test_local_self_report_and_corrupt_archive_cannot_replace_backend_bytes(self):
        payload = b'{"schema": 1, "success": true}\n'
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("matrix-evidence.json", payload)
        raw = stream.getvalue()
        backend = {"size_in_bytes": len(raw), "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
        validate_matrix_archive(raw, backend, payload)
        with self.assertRaises(ValueError):
            validate_matrix_archive(raw, backend, payload.replace(b"true", b"false"))
        corrupted = bytearray(raw)
        corrupted[40] ^= 1
        with self.assertRaises(ValueError):
            validate_matrix_archive(bytes(corrupted), backend, payload)
        with self.assertRaises(ValueError):
            validate_matrix_archive(raw, {**backend, "size_in_bytes": 0}, payload)


if __name__ == "__main__":
    unittest.main()
