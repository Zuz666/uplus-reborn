"""Release precondition boundaries; these tests never call GitHub or mutate tags."""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import publish_release
from github_api import GitHubOperationError
from publish_release import ASSETS, publish, validate_remote_state


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


class DraftReleaseBackend:
    """Model draft identity separately from GitHub's published-by-tag lookup."""

    def __init__(self, directory, metadata):
        self.directory = directory
        self.metadata = metadata
        self.release = None
        self.assets = {}
        self.writes = []
        self.source_checks = 0
        self.draft_changes = {}
        self.created_changes = {}
        self.published_changes = {}
        self.corrupt_asset = False
        self.fail_creation = False
        self.fail_refresh = False
        self.fail_publication = False

    def pages(self, path):
        return []

    def tag_commit(self, repository, tag):
        return self.metadata["commit"]

    def create(self):
        self.writes.append("create")
        if self.fail_creation:
            raise GitHubOperationError("Creation outcome unknown")
        self.release = {
            "id": 7123, "tag_name": self.metadata["tag"], "draft": True,
            "prerelease": False, "assets": [], "html_url": "https://example.invalid/release",
        }
        return {**deepcopy(self.release), **self.created_changes}

    def api(self, path, *, payload=None):
        if path.endswith("/git/refs") and payload is not None:
            self.writes.append("tag")
            return {}
        if path.endswith("/releases") and payload is not None:
            return self.create()
        if "/releases/tags/" in path:
            if self.release is None or self.release["draft"]:
                raise GitHubOperationError("GitHub api operation failed (HTTP 404)")
            return {**deepcopy(self.release), **self.published_changes}
        if path.endswith("/releases/7123"):
            return {**deepcopy(self.release), **self.draft_changes}
        raise AssertionError(f"Unexpected GitHub operation: {path}")

    def gh(self, *args):
        if args[:2] == ("release", "upload"):
            self.writes.append("upload")
            self.assets = {name: (self.directory / name).read_bytes() for name in ASSETS}
            self.release["assets"] = [{"name": name} for name in self.assets]
            return ""
        if args[:2] == ("release", "download"):
            target = Path(args[args.index("--dir") + 1])
            for name, data in self.assets.items():
                (target / name).write_bytes(data + (b"corrupt" if self.corrupt_asset else b""))
            return ""
        if args[:2] == ("release", "edit"):
            self.writes.append("publish")
            if self.fail_publication:
                raise GitHubOperationError("Publication outcome unknown")
            self.release["draft"] = False
            return ""
        raise AssertionError(f"Unexpected GitHub command: {args[:2]}")

    def source_evidence(self, *args):
        self.source_checks += 1
        if self.fail_refresh and self.source_checks > 1:
            raise ValueError("Source evidence changed")
        return {
            "selection": {
                "branch": {
                    "name": "master", "commit": {"sha": self.metadata["commit"]},
                    "protected": True,
                    "protection": {"required_status_checks": {"contexts": ["CI"]}},
                },
                "checks": [{
                    "name": "CI", "head_sha": self.metadata["commit"],
                    "status": "completed", "conclusion": "success",
                }],
                "source": {"id": 456, "run_attempt": 1},
            },
            "candidate": {"sha256": "b" * 64},
        }


class PublicationTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for name in ASSETS:
            (self.directory / name).write_bytes(f"verified {name}".encode())
        (self.directory / "release-notes.md").write_text("Release notes\n", encoding="utf-8")
        self.metadata = {"commit": "a" * 40, "version": "1.0.3", "tag": "v1.0.3"}
        self.backend = DraftReleaseBackend(self.directory, self.metadata)
        for name in ("pages", "api", "gh", "tag_commit"):
            self.enterContext(patch.object(publish_release.github, name, getattr(self.backend, name)))
        self.enterContext(patch.object(
            publish_release.release_evidence, "verify_source_evidence", self.backend.source_evidence,
        ))

    def publish(self, *, approve=True):
        return publish(
            self.directory, "example/integration", self.metadata, self.directory,
            selection=self.directory / "selection.json",
            matrix=self.directory / "matrix.json", approve=approve,
        )

    def test_created_draft_can_be_verified_then_published_without_tag_lookup(self):
        result = self.publish()
        self.assertTrue(result["published"])
        self.assertFalse(self.backend.release["draft"])
        self.assertEqual(self.backend.assets, {
            name: (self.directory / name).read_bytes() for name in ASSETS
        })
        self.assertEqual(self.backend.source_checks, 2)
        self.assertEqual(self.backend.writes, ["tag", "create", "upload", "publish"])

    def test_changed_draft_is_not_published(self):
        for changes in (
            {"id": 7124}, {"tag_name": "v9.0.0"}, {"draft": False},
            {"prerelease": True}, {"prerelease": None}, {"assets": []},
        ):
            with self.subTest(changes=changes):
                self.backend.draft_changes = changes
                self.backend.writes.clear()
                with self.assertRaisesRegex(ValueError, "Draft/tag/asset state changed"):
                    self.publish()
                self.assertTrue(self.backend.release["draft"])
                self.assertEqual(self.backend.writes, ["tag", "create", "upload"])

    def test_invalid_created_identity_stops_before_upload(self):
        for changes in (
            {"id": None}, {"id": True}, {"id": 0}, {"id": "7123"},
            {"tag_name": "v9.0.0"}, {"draft": False}, {"prerelease": True},
        ):
            with self.subTest(changes=changes):
                self.backend.created_changes = changes
                self.backend.writes.clear()
                with self.assertRaisesRegex(ValueError, "Created draft identity differs"):
                    self.publish()
                self.assertEqual(self.backend.writes, ["tag", "create"])

    def test_changed_uploaded_bytes_block_publication(self):
        self.backend.corrupt_asset = True
        with self.assertRaisesRegex(ValueError, "Uploaded asset differs"):
            self.publish()
        self.assertTrue(self.backend.release["draft"])
        self.assertEqual(self.backend.writes, ["tag", "create", "upload"])

    def test_source_change_after_upload_blocks_publication(self):
        self.backend.fail_refresh = True
        with self.assertRaisesRegex(ValueError, "Source evidence changed"):
            self.publish()
        self.assertTrue(self.backend.release["draft"])
        self.assertEqual(self.backend.writes, ["tag", "create", "upload"])

    def test_uncertain_creation_or_publication_is_not_retried(self):
        self.backend.fail_creation = True
        with self.assertRaisesRegex(GitHubOperationError, "Creation outcome unknown"):
            self.publish()
        self.assertEqual(self.backend.writes, ["tag", "create"])
        self.backend.fail_creation = False
        self.backend.fail_publication = True
        self.backend.writes.clear()
        with self.assertRaisesRegex(GitHubOperationError, "Publication outcome unknown"):
            self.publish()
        self.assertEqual(self.backend.writes, ["tag", "create", "upload", "publish"])

    def test_published_lookup_cannot_substitute_another_release(self):
        self.backend.published_changes = {"id": 7124}
        with self.assertRaisesRegex(ValueError, "Published release/tag acceptance failed"):
            self.publish()
        self.assertEqual(self.backend.writes, ["tag", "create", "upload", "publish"])

    def test_inspection_without_approval_makes_no_writes(self):
        result = self.publish(approve=False)
        self.assertFalse(result["published"])
        self.assertEqual(self.backend.writes, [])


if __name__ == "__main__":
    unittest.main()
