"""Conservative discovery timing selection through the public CLI and real Git."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).with_name("discovery_acceptance.py").resolve()


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "Discovery selection test")
        self.git("config", "user.email", "selection@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.change("README.md", "Initial documentation\n")
        self.change("custom_components/uplus_reborn/discovery_interval.py", "initial\n")
        self.base = self.commit("Initial")
        self.git("checkout", "--quiet", "-b", "pull-request")
        self.change("docs/development-verification.md", "Documentation only\n")
        self.head = self.commit("Document verification")
        self.candidate = self.head
        self.event = {
            "number": 66,
            "repository": {"full_name": "owner/repo"},
            "pull_request": {
                "number": 66,
                "base": {"sha": self.base, "repo": {"full_name": "owner/repo"}},
                "head": {"sha": self.head},
            },
        }

    def git(self, *arguments):
        result = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def change(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def select(self, *arguments, event_name="pull_request", environment=None):
        event_file = self.root / "event.json"
        event_file.write_text(json.dumps(self.event), encoding="utf-8")
        receipt = self.root / "selection.json"
        github_output = self.root / "github-output"
        github_output.write_text("", encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith("GITHUB_")}
        env.update({
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(event_file),
            "GITHUB_SHA": self.candidate,
            "GITHUB_REF": "refs/pull/66/head",
            "GITHUB_REPOSITORY": "owner/repo",
        })
        env.update(environment or {})
        result = subprocess.run(
            [sys.executable, str(TOOL), "select", "--repo", str(self.repo),
             "--output", str(receipt), "--github-output", str(github_output), *arguments],
            capture_output=True, text=True, encoding="utf-8", check=False, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        selection = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(github_output.read_text(encoding="utf-8"),
                         f"discovery_timing={selection['mode']}\n")
        return selection

    def test_complete_documentation_only_pr_selects_fast_and_binds_commit_identities(self):
        result = self.select()
        self.assertEqual(result["mode"], "fast")
        self.assertEqual(result["reason"], "unrelated_changes")
        self.assertEqual(result["tested_commit"], self.head)
        self.assertEqual(result["base_commit"], self.base)
        self.assertEqual(result["head_commit"], self.head)
        self.assertEqual(result["comparison_commit"], self.base)
        self.assertEqual(result["candidate_kind"], "head")

    def bind_head(self):
        self.head = self.git("rev-parse", "HEAD")
        self.candidate = self.head
        self.event["pull_request"]["head"]["sha"] = self.head

    def test_runtime_callers_dependencies_environment_and_verification_require_full(self):
        docs_head = self.head
        for name in (
            "custom_components/uplus_reborn/discovery_interval.py",
            "custom_components/uplus_reborn/config_flow.py",
            "custom_components/uplus_reborn/discovery_preview.py",
            "custom_components/uplus_reborn/const.py",
            "custom_components/uplus_reborn/protocol/_protocol.py",
            "custom_components/uplus_reborn/manifest.json",
            "tools/discovery_fixture.py",
            "tools/ha_smoke.py",
            "tools/discovery_acceptance.py",
            "tools/Dockerfile",
            "tests/test_discovery_interval.py",
            ".github/workflows/delivery.yml",
            ".github/actions/build-image/action.yml",
            "pyproject.toml",
            "uv.lock",
            ".python-version",
        ):
            with self.subTest(path=name):
                self.git("checkout", "--quiet", "--detach", docs_head)
                self.change(name, "Changed relevant input\n")
                self.commit("Change an acceptance input")
                self.bind_head()
                result = self.select()
                self.assertEqual(result["mode"], "full")
                self.assertEqual(result["reason"], "relevant_changes")

    def test_earlier_relevant_commit_is_not_hidden_by_last_documentation_commit(self):
        self.change("custom_components/uplus_reborn/discovery_interval.py", "changed\n")
        self.commit("Change discovery")
        self.change("README.md", "Last commit only changes documentation\n")
        self.commit("Document change")
        self.bind_head()
        self.assertEqual(self.select()["reason"], "relevant_changes")

    def test_reverted_relevant_commit_still_requires_full(self):
        self.change("custom_components/uplus_reborn/discovery_interval.py", "changed\n")
        self.commit("Change discovery")
        self.change("custom_components/uplus_reborn/discovery_interval.py", "initial\n")
        self.change("README.md", "Document the reverted experiment\n")
        self.commit("Revert discovery and document")
        self.bind_head()
        self.assertEqual(self.select()["reason"], "relevant_changes")

    def test_renaming_runtime_into_documentation_includes_the_deleted_side(self):
        self.git("mv", "custom_components/uplus_reborn/discovery_interval.py", "docs/moved.md")
        self.commit("Move runtime out of package")
        self.bind_head()
        self.assertEqual(self.select()["reason"], "relevant_changes")

    def test_renaming_documentation_into_runtime_includes_the_added_side(self):
        self.git("mv", "docs/development-verification.md", "custom_components/uplus_reborn/caller.py")
        self.commit("Move documentation into runtime")
        self.bind_head()
        self.assertEqual(self.select()["reason"], "relevant_changes")

    def test_documentation_rename_remains_fast(self):
        self.git("mv", "docs/development-verification.md", "docs/renamed.md")
        self.commit("Rename documentation")
        self.bind_head()
        self.assertEqual(self.select()["mode"], "fast")

    def test_runtime_deletion_requires_full(self):
        self.git("rm", "custom_components/uplus_reborn/discovery_interval.py")
        self.commit("Remove runtime")
        self.bind_head()
        self.assertEqual(self.select()["reason"], "relevant_changes")

    def test_documentation_deletion_remains_fast(self):
        self.git("rm", "README.md")
        self.commit("Remove documentation")
        self.bind_head()
        self.assertEqual(self.select()["mode"], "fast")

    def test_unknown_paths_cannot_authorize_fast_even_inside_documentation(self):
        docs_head = self.head
        for name in ("new-runtime.py", "docs/helper.py", ".unknown/config", "new-area/README.md"):
            with self.subTest(path=name):
                self.git("checkout", "--quiet", "--detach", docs_head)
                self.change(name, "Unknown impact\n")
                self.commit("Add an unclassified input")
                self.bind_head()
                result = self.select()
                self.assertEqual(result["mode"], "full")
                self.assertEqual(result["reason"], "unknown_changes")

    def make_merge_candidate(self):
        self.git("checkout", "--quiet", "--detach", self.base)
        self.change("README.md", "Base branch advanced\n")
        self.base = self.commit("Advance base")
        self.git("merge", "--quiet", "--no-ff", "-m", "Synthetic PR merge", self.head)
        self.candidate = self.git("rev-parse", "HEAD")
        self.event["pull_request"]["base"]["sha"] = self.base
        self.event["pull_request"]["merge_commit_sha"] = self.candidate

    def test_actual_synthetic_merge_is_bound_separately_from_pr_head(self):
        self.make_merge_candidate()
        result = self.select(environment={"GITHUB_REF": "refs/pull/66/merge"})
        self.assertEqual(result["mode"], "fast")
        self.assertEqual(result["tested_commit"], self.candidate)
        self.assertNotEqual(result["tested_commit"], self.head)
        self.assertEqual(result["candidate_kind"], "merge")
        self.assertEqual(result["head_commit"], self.head)
        self.assertEqual(result["base_commit"], self.base)

    def test_relevant_merge_resolution_not_present_in_head_still_requires_full(self):
        self.make_merge_candidate()
        self.change("custom_components/uplus_reborn/discovery_interval.py", "merge resolution\n")
        self.git("add", "--all")
        self.git("commit", "--quiet", "--amend", "--no-edit")
        self.candidate = self.git("rev-parse", "HEAD")
        self.event["pull_request"]["merge_commit_sha"] = self.candidate
        self.assertEqual(self.select(environment={"GITHUB_REF": "refs/pull/66/merge"})["reason"],
                         "relevant_changes")

    def test_stale_candidate_identity_cannot_use_head_classification(self):
        result = self.select(environment={"GITHUB_SHA": self.base})
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["reason"], "stale_comparison")

    def test_head_checkout_is_not_misrepresented_as_tested_merge(self):
        result = self.select(environment={"GITHUB_REF": "refs/pull/66/merge"})
        self.assertEqual(result["reason"], "stale_comparison")

    def test_stale_merge_base_head_or_event_merge_identity_requires_full(self):
        self.make_merge_candidate()
        correct = json.loads(json.dumps(self.event))
        for field in ("base", "head", "merge_commit_sha"):
            with self.subTest(field=field):
                self.event = json.loads(json.dumps(correct))
                if field == "merge_commit_sha":
                    self.event["pull_request"][field] = self.head
                else:
                    self.event["pull_request"][field]["sha"] = self.candidate
                result = self.select(environment={"GITHUB_REF": "refs/pull/66/merge"})
                self.assertEqual(result["reason"], "stale_comparison")

    def test_foreign_repository_or_pr_number_requires_full(self):
        for environment in (
            {"GITHUB_REPOSITORY": "other/repo"},
            {"GITHUB_REF": "refs/pull/67/head"},
        ):
            with self.subTest(environment=environment):
                self.assertEqual(self.select(environment=environment)["reason"], "stale_comparison")

    def test_missing_immutable_object_requires_full(self):
        self.event["pull_request"]["base"]["sha"] = "a" * 40
        result = self.select()
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["reason"], "incomplete_comparison")

    def test_shallow_history_cannot_authorize_fast(self):
        (self.repo / ".git" / "shallow").write_text(self.head + "\n", encoding="ascii")
        result = self.select()
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["reason"], "incomplete_comparison")

    def test_missing_or_malformed_event_data_requires_full(self):
        for event in ({}, {"pull_request": None}, []):
            with self.subTest(event=event):
                self.event = event
                result = self.select()
                self.assertEqual(result["mode"], "full")
                self.assertEqual(result["reason"], "unavailable_comparison")
        result = self.select(environment={"GITHUB_EVENT_PATH": str(self.root / "absent.json")})
        self.assertEqual(result["reason"], "unavailable_comparison")

    def test_default_manual_master_and_other_events_keep_full(self):
        for event in ("", "workflow_dispatch", "push", "workflow_call", "pull_request_target"):
            with self.subTest(event=event):
                result = self.select(event_name=event, environment={"GITHUB_REF": "refs/heads/master"})
                self.assertEqual(result["mode"], "full")
                self.assertEqual(result["reason"], "non_pull_request")

    def test_malformed_event_and_invalid_commit_ids_never_authorize_fast(self):
        bad_event = self.root / "malformed-event.json"
        bad_event.write_text("{not JSON", encoding="utf-8")
        result = self.select(environment={"GITHUB_EVENT_PATH": str(bad_event)})
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["reason"], "unavailable_comparison")
        self.event["pull_request"]["base"]["sha"] = "master"
        result = self.select()
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["reason"], "unavailable_comparison")

    def test_unrelated_histories_cannot_authorize_fast(self):
        tree = self.git("rev-parse", f"{self.base}^{{tree}}")
        unrelated = self.git("commit-tree", tree, "-m", "Unrelated base history")
        self.event["pull_request"]["base"]["sha"] = unrelated
        result = self.select()
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["reason"], "incomplete_comparison")

    def test_force_full_overrides_complete_unrelated_pr(self):
        result = self.select("--force-full")
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["reason"], "forced_full")


if __name__ == "__main__":
    unittest.main()
