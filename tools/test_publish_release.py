"""Release precondition boundaries; these tests never call GitHub or mutate tags."""

from __future__ import annotations

import unittest
from copy import deepcopy

from publish_release import validate_remote_state


class PublicationGateTests(unittest.TestCase):
    def setUp(self):
        self.metadata = {"commit": "a" * 40, "version": "1.0.0", "tag": "v1.0.0"}
        self.branch = {
            "name": "master", "commit": {"sha": self.metadata["commit"]}, "protected": True,
            "protection": {"required_status_checks": {
                "contexts": ["CI"], "checks": [{"context": "CI", "app_id": 123}],
            }},
        }
        self.checks = [{
            "name": "CI", "conclusion": "success", "status": "completed",
            "head_sha": self.metadata["commit"], "app": {"id": 123},
        }]

    def test_another_branch_cannot_publish_the_accepted_sha(self):
        for name in ("main", "release/1.0.0", "feature/change"):
            with self.subTest(branch=name), self.assertRaises(ValueError):
                validate_remote_state(
                    self.metadata, {**self.branch, "name": name}, [], [], self.checks,
                )

    def test_an_accepted_old_sha_cannot_publish_after_master_advances(self):
        self.branch["commit"]["sha"] = "b" * 40
        with self.assertRaises(ValueError):
            validate_remote_state(self.metadata, self.branch, [], [], self.checks)

    def test_removed_branch_protection_or_gate_is_not_a_release_bypass(self):
        unprotected = deepcopy(self.branch)
        unprotected["protected"] = False
        ungated = deepcopy(self.branch)
        ungated["protection"] = {}
        for branch in (unprotected, ungated):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                validate_remote_state(self.metadata, branch, [], [], self.checks)

    def test_failure_wrong_sha_or_wrong_app_cannot_satisfy_required_ci(self):
        variants = (
            {"conclusion": "failure"},
            {"head_sha": "b" * 40},
            {"app": {"id": 456}},
            {"status": "in_progress"},
        )
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_remote_state(self.metadata, self.branch, [], [], [{**self.checks[0], **changes}])

    def test_existing_tag_or_even_a_draft_is_not_overwritten(self):
        for tags, releases in (([{"name": "v1.0.0"}], []), ([], [{"tag_name": "v1.0.0", "draft": True}])):
            with self.subTest(tags=tags, releases=releases), self.assertRaises(ValueError):
                validate_remote_state(self.metadata, self.branch, tags, releases, self.checks)

    def test_version_rollback_or_undefined_prerelease_order_is_rejected(self):
        for name in ("v2.0.0", "v1.0.0-rc.1"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_remote_state(self.metadata, self.branch, [{"name": name}], [], self.checks)


if __name__ == "__main__":
    unittest.main()
