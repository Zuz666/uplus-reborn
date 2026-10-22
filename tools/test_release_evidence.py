"""Consumer gates reject incomplete or misbound Core evidence; no network calls."""
from __future__ import annotations

import copy
import unittest

from release_evidence import SCENARIOS, artifact_metadata, validate_core_receipts


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


if __name__ == "__main__":
    unittest.main()
