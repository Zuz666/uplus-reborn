"""Consumer-visible release archive and metadata regressions; no HA dependencies."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

from release import previous_stable, release_metadata, verify_candidate

TOOL = Path(__file__).with_name("release.py")
PACKAGE = "custom_components/uplus_reborn"


class StableBaselineTests(unittest.TestCase):
    def test_latest_older_stable_uses_numeric_order_and_ignores_unpublished_versions(self):
        releases = [
            {"tag_name": "v1.0.9"}, {"tag_name": "v1.0.10"},
            {"tag_name": "v1.1.0"}, {"tag_name": "v2.0.0"},
            {"tag_name": "v1.0.12", "prerelease": True},
            {"tag_name": "v1.0.11", "draft": True},
        ]
        self.assertEqual(previous_stable(releases, "1.1.0"), "v1.0.10")

    def test_first_release_has_no_fabricated_upgrade_baseline(self):
        self.assertIsNone(previous_stable([{"tag_name": "v1.0.0"}], "1.0.0"))


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="uplus release [test] ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Release test")
        self.git("config", "user.email", "release-test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.files = {
            f"{PACKAGE}/manifest.json": json.dumps({"domain": "uplus_reborn", "version": "1.0.0"}),
            f"{PACKAGE}/__init__.py": "VALUE = 'committed'\n",
            f"{PACKAGE}/translations/en.json": "{}\n",
            f"{PACKAGE}/LICENSE": "MIT License\nPermission is hereby granted\n",
            "pyproject.toml": '[project]\nname = "uplus-reborn"\nversion = "1.0.0"\n',
            "uv.lock": 'version = 1\n[[package]]\nname = "uplus-reborn"\nversion = "1.0.0"\n',
            "CHANGELOG.md": "# Changelog\n\n## [1.0.0] - 2026-10-21\n\n- Initial appliance integration.\n",
        }
        for name, content in self.files.items():
            self.put(name, content)
        self.commit = self.save()

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def put(self, name, content):
        target = self.repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")

    def save(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "test fixture")
        return self.git("rev-parse", "HEAD")

    def cli(self, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(TOOL), *map(str, args), "--repo", str(self.repo)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_prepare_preserves_dependency_versions_and_unrelated_dirty_work(self):
        self.put("uv.lock", self.files["uv.lock"] + 'source = { virtual = "." }\n'
                 '[[package]]\nname = "other"\nversion = "1.0.0"\n')
        self.save()
        self.put(f"{PACKAGE}/__init__.py", "VALUE = 'owner work'\n")
        notes = self.root / "owner release notes.md"
        notes.write_text("- Preserve actual user profiles.\n", encoding="utf-8")
        self.cli("prepare", "--version", "1.0.1", "--date", "2026-10-22", "--notes", notes)
        lock = tomllib.loads((self.repo / "uv.lock").read_text())
        self.assertEqual(next(p["version"] for p in lock["package"] if p["name"] == "other"), "1.0.0")
        self.assertEqual((self.repo / PACKAGE / "__init__.py").read_text(), "VALUE = 'owner work'\n")
        metadata = release_metadata(self.repo, self.save(), "1.0.1")
        self.assertEqual(metadata["notes"], "- Preserve actual user profiles.\n")
        self.assertIn("## [1.0.0]", (self.repo / "CHANGELOG.md").read_text())

    def test_prepare_refuses_dirty_metadata_without_partial_writes(self):
        self.put("pyproject.toml", self.files["pyproject.toml"] + "# owner edit\n")
        notes = self.root / "notes.md"
        notes.write_text("- Preserve actual user profiles.\n", encoding="utf-8")
        paths = [self.repo / p for p in (f"{PACKAGE}/manifest.json", "pyproject.toml", "uv.lock", "CHANGELOG.md")]
        before = [p.read_bytes() for p in paths]
        self.cli("prepare", "--version", "1.0.1", "--date", "2026-10-22", "--notes", notes, expected=1)
        self.assertEqual([p.read_bytes() for p in paths], before)

    def test_zip_installs_at_component_root_from_selected_commit_only(self):
        self.put(f"{PACKAGE}/__init__.py", "VALUE = 'dirty'\n")
        self.put(f"{PACKAGE}/untracked-secret.txt", "not for publication")
        self.put("pyproject.toml", self.files["pyproject.toml"].replace("1.0.0", "9.0.0"))
        output = self.root / "release"
        result = self.cli("build", "--commit", self.commit, "--version", "1.0.0", "--output", output)
        provenance = json.loads(result.stdout)
        installed = self.root / "installed" / PACKAGE
        with zipfile.ZipFile(output / "uplus_reborn.zip") as archive:
            archive.extractall(installed)
            self.assertNotIn("untracked-secret.txt", archive.namelist())
            self.assertFalse(any(n.startswith("custom_components/") for n in archive.namelist()))
        self.assertEqual(json.loads((installed / "manifest.json").read_text())["version"], "1.0.0")
        self.assertEqual((installed / "__init__.py").read_text(), "VALUE = 'committed'\n")
        self.assertEqual(provenance["commit"], self.commit)
        self.assertEqual(hashlib.sha256((output / "uplus_reborn.zip").read_bytes()).hexdigest(), provenance["sha256"])
        second = self.root / "second"
        self.cli("build", "--commit", self.commit, "--output", second)
        self.assertEqual((output / "uplus_reborn.zip").read_bytes(), (second / "uplus_reborn.zip").read_bytes())

    def test_each_metadata_mismatch_is_rejected_before_creating_output(self):
        for name in (f"{PACKAGE}/manifest.json", "pyproject.toml", "uv.lock"):
            with self.subTest(name=name):
                self.put(name, self.files[name].replace("1.0.0", "1.0.1"))
                commit = self.save()
                output = self.root / ("bad-" + Path(name).name)
                self.cli("build", "--commit", commit, "--output", output, expected=1)
                self.assertFalse(output.exists())
                self.put(name, self.files[name])
                self.save()

    def test_requested_version_and_invalid_commit_fail_closed(self):
        self.cli("check", "--commit", self.commit, "--version", "1.0.1", expected=1)
        self.cli("check", "--commit", "no-such-revision", expected=1)

    def test_missing_ambiguous_or_empty_release_notes_are_rejected(self):
        variants = (
            "# Changelog\n",
            self.files["CHANGELOG.md"] + "\n## [1.0.0] - 2026-10-22\n\n- Duplicate.\n",
            "# Changelog\n\n## [1.0.0] - 2026-10-21\n",
            "# Changelog\n\n## [1.0.0] - 2026-02-30\n\n- Invalid date.\n",
        )
        for notes in variants:
            with self.subTest(notes=notes):
                self.put("CHANGELOG.md", notes)
                self.cli("check", "--commit", self.save(), expected=1)

    def test_notes_do_not_include_other_versions(self):
        self.put("CHANGELOG.md", self.files["CHANGELOG.md"] + "\n## [0.9.0] - 2026-10-20\n\n- Older change.\n")
        result = self.cli("check", "--commit", self.save())
        self.assertEqual(json.loads(result.stdout)["notes"], "- Initial appliance integration.\n")

    def test_incomplete_package_is_not_left_as_a_candidate(self):
        (self.repo / PACKAGE / "__init__.py").unlink()
        output = self.root / "incomplete"
        self.cli("build", "--commit", self.save(), "--output", output, expected=1)
        self.assertFalse(output.exists())

    def test_occupied_output_is_preserved(self):
        output = self.root / "existing"
        output.mkdir()
        receipt = output / "prior-proof.txt"
        receipt.write_text("preserve me", encoding="utf-8")
        self.cli("build", "--commit", self.commit, "--output", output, expected=1)
        self.assertEqual(receipt.read_text(), "preserve me")

    def test_archive_attribute_omission_cannot_create_a_partial_release(self):
        self.put(".gitattributes", f"{PACKAGE}/translations/en.json export-ignore\n")
        output = self.root / "omitted"
        self.cli("build", "--commit", self.save(), "--output", output, expected=1)
        self.assertFalse(output.exists())

    def test_forged_source_commit_cannot_relabel_a_different_package(self):
        original = release_metadata(self.repo, self.commit)
        self.put(f"{PACKAGE}/__init__.py", "VALUE = 'different committed package'\n")
        changed = self.save()
        output = self.root / "forged"
        self.cli("build", "--commit", changed, "--output", output)
        for name in ("bundle/artifact.json", "release.json"):
            path = output / name
            data = json.loads(path.read_text(encoding="utf-8"))
            data["commit"] = self.commit
            path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            verify_candidate(self.repo, output, original)


if __name__ == "__main__":
    unittest.main()
