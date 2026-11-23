#!/usr/bin/env python3
"""Select reduced discovery timing only for completely bound, unrelated PRs.

Git objects, not the last commit or the event's optional file list, supply the
comparison. This tool never fetches, uses a token, or queries a remote API.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SEMVER = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
UNRELATED_FILES = frozenset({"README.md", "CONTRIBUTING.md", "CHANGELOG.md", "SECURITY.md", "LICENSE"})
UNRELATED_TOOLS = frozenset({
    "tools/check-docs.mjs",
    "tools/check-docs.test.mjs",
    "tools/closed-fences.cjs",
    "tools/ci_summary.py",
    "tools/test_ci_summary.py",
    "tools/generate_brand_icon.py",
    "tools/github_api.py",
    "tools/hacs-fixture.json",
    "tools/hacs_acceptance.py",
    "tools/hacs_fixture.py",
    "tools/test_hacs_acceptance.py",
    "tools/pr-labels.cjs",
    "tools/pr-labels.test.cjs",
    "tools/publish_release.py",
    "tools/test_publish_release.py",
    "tools/release.py",
    "tools/test_release.py",
    "tools/test_release_operations.py",
    "tools/release_evidence.py",
    "tools/test_release_evidence.py",
    "tools/test_native_lint.py",
    "tools/upgrade_smoke.py",
    "tools/test_upgrade_smoke.py",
})
VERSION_METADATA_FILES = frozenset({
    "custom_components/uplus_reborn/manifest.json",
    "pyproject.toml",
    "uv.lock",
})
RELEVANT_FILES = frozenset({
    "pyproject.toml", "uv.lock", "package.json", "package-lock.json", "hacs.json",
    "Dockerfile", "docker-compose.yml", "compose.yml", ".python-version",
    ".gitmodules", ".gitattributes", ".gitignore",
})


class ComparisonUnavailable(Exception):
    """Comparison cannot safely authorize reduced real-time proof."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _git(repo: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(repo), *arguments],
            capture_output=True, check=False,
        )
    except OSError as error:
        raise ComparisonUnavailable("unavailable_comparison") from error
    if result.returncode:
        raise ComparisonUnavailable("incomplete_comparison")
    return result.stdout


def _commit(repo: Path, revision: str) -> str:
    value = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").decode("ascii").strip()
    if not COMMIT.fullmatch(value):
        raise ComparisonUnavailable("incomplete_comparison")
    return value


def _path_statuses(output: bytes) -> list[tuple[str, str]]:
    """Decode NUL-separated status/path pairs; renames are delete plus add."""
    if not output:
        return []
    parts = output.split(b"\0")
    if parts.pop() != b"" or len(parts) % 2:
        raise ComparisonUnavailable("incomplete_comparison")
    changes: list[tuple[str, str]] = []
    for index in range(0, len(parts), 2):
        raw_status = parts[index]
        if raw_status not in {b"A", b"D", b"M"}:
            raise ComparisonUnavailable("unknown_changes")
        try:
            path = parts[index + 1].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ComparisonUnavailable("unknown_changes") from error
        if not path or any(part in {"", ".", ".."} for part in path.split("/")):
            raise ComparisonUnavailable("unknown_changes")
        changes.append((raw_status.decode("ascii"), path))
    return changes


def _is_version_only_diff(repo: Path, left: str, right: str, path: str) -> bool:
    """Return True only if committed `path` changed solely its SemVer version field."""
    try:
        old_text = _git(repo, "show", f"{left}:{path}").decode("utf-8")
        new_text = _git(repo, "show", f"{right}:{path}").decode("utf-8")
    except (ComparisonUnavailable, UnicodeDecodeError):
        return False
    if path == "custom_components/uplus_reborn/manifest.json":
        try:
            old_obj, new_obj = json.loads(old_text), json.loads(new_text)
        except ValueError:
            return False
        if not isinstance(old_obj, dict) or not isinstance(new_obj, dict):
            return False
        old_ver, new_ver = old_obj.get("version"), new_obj.get("version")
        if not (
            isinstance(old_ver, str)
            and isinstance(new_ver, str)
            and SEMVER.fullmatch(old_ver)
            and SEMVER.fullmatch(new_ver)
        ):
            return False
        return {**old_obj, "version": new_ver} == new_obj
    if path == "pyproject.toml":
        try:
            old_obj, new_obj = tomllib.loads(old_text), tomllib.loads(new_text)
        except tomllib.TOMLDecodeError:
            return False
        old_proj, new_proj = old_obj.get("project"), new_obj.get("project")
        if not isinstance(old_proj, dict) or not isinstance(new_proj, dict):
            return False
        if old_proj.get("name") != "uplus-reborn" or new_proj.get("name") != "uplus-reborn":
            return False
        old_ver, new_ver = old_proj.get("version"), new_proj.get("version")
        if not (
            isinstance(old_ver, str)
            and isinstance(new_ver, str)
            and SEMVER.fullmatch(old_ver)
            and SEMVER.fullmatch(new_ver)
        ):
            return False
        old_proj["version"] = new_ver
        return old_obj == new_obj
    if path == "uv.lock":
        try:
            old_obj, new_obj = tomllib.loads(old_text), tomllib.loads(new_text)
        except tomllib.TOMLDecodeError:
            return False
        old_pkgs, new_pkgs = old_obj.get("package"), new_obj.get("package")
        if not isinstance(old_pkgs, list) or not isinstance(new_pkgs, list):
            return False
        old_roots = [p for p in old_pkgs if isinstance(p, dict) and p.get("name") == "uplus-reborn"]
        new_roots = [p for p in new_pkgs if isinstance(p, dict) and p.get("name") == "uplus-reborn"]
        if len(old_roots) != 1 or len(new_roots) != 1:
            return False
        old_root, new_root = old_roots[0], new_roots[0]
        if old_root.get("source") != {"virtual": "."} or new_root.get("source") != {"virtual": "."}:
            return False
        old_ver, new_ver = old_root.get("version"), new_root.get("version")
        if not (
            isinstance(old_ver, str)
            and isinstance(new_ver, str)
            and SEMVER.fullmatch(old_ver)
            and SEMVER.fullmatch(new_ver)
        ):
            return False
        old_root["version"] = new_ver
        return old_obj == new_obj
    return False


def _classify_changes(repo: Path, changes: list[tuple[str | None, str, str, str]]) -> str:
    if not changes:
        return "unknown_changes"
    has_unknown = False
    for left, right, status, path in changes:
        if path in VERSION_METADATA_FILES:
            if status == "M" and left is not None and _is_version_only_diff(repo, left, right, path):
                continue
            return "relevant_changes"
        if path in UNRELATED_TOOLS:
            continue
        # Remaining runtime and verification closures are relevant, including callers,
        # shared code, bootstrap/environment inputs and selector/workflow changes.
        if path.startswith(("custom_components/", "tests/", "tools/", ".github/")) or path in RELEVANT_FILES:
            return "relevant_changes"
        if path in UNRELATED_FILES or path == "docs/LICENSE":
            continue
        if path.startswith("docs/") and path.endswith(".md"):
            continue
        has_unknown = True
    return "unknown_changes" if has_unknown else "unrelated_changes"


def _pr_comparison(repo: Path, environment: dict[str, str], selection: dict) -> str:
    try:
        event = json.loads(Path(environment["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        pr = event["pull_request"]
        base, head = pr["base"]["sha"], pr["head"]["sha"]
        number = event["number"]
        repository = event["repository"]["full_name"]
        base_repository = pr["base"]["repo"]["full_name"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ComparisonUnavailable("unavailable_comparison") from error
    if not all(isinstance(value, str) and COMMIT.fullmatch(value) for value in (base, head)):
        raise ComparisonUnavailable("unavailable_comparison")
    selection.update(base_commit=base, head_commit=head)
    if (type(number) is not int or number <= 0 or pr.get("number") != number
            or not repository or repository != environment.get("GITHUB_REPOSITORY")
            or base_repository != repository):
        raise ComparisonUnavailable("stale_comparison")
    candidate = selection["tested_commit"]
    if candidate != environment.get("GITHUB_SHA"):
        raise ComparisonUnavailable("stale_comparison")
    ref = environment.get("GITHUB_REF")
    if ref == f"refs/pull/{number}/head" and candidate == head:
        selection["candidate_kind"] = "head"
    elif ref == f"refs/pull/{number}/merge" and candidate != head:
        parents = _git(repo, "show", "-s", "--format=%P", candidate).decode("ascii").strip().split()
        if parents != [base, head] or pr.get("merge_commit_sha") not in (None, candidate):
            raise ComparisonUnavailable("stale_comparison")
        selection["candidate_kind"] = "merge"
    else:
        raise ComparisonUnavailable("stale_comparison")
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ComparisonUnavailable("incomplete_comparison")
    for revision in (base, head):
        if _commit(repo, revision) != revision:
            raise ComparisonUnavailable("stale_comparison")
    ancestors = _git(repo, "merge-base", "--all", base, head).decode("ascii").split()
    if len(ancestors) != 1 or not COMMIT.fullmatch(ancestors[0]):
        raise ComparisonUnavailable("incomplete_comparison")
    comparison = ancestors[0]
    selection["comparison_commit"] = comparison
    changes: list[tuple[str | None, str, str, str]] = []
    for left, right in ((comparison, head), (base, candidate)):
        for status, path in _path_statuses(_git(
            repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
            "--name-status", "-z", left, right, "--",
        )):
            changes.append((left, right, status, path))
    # Include every PR commit, even relevant edits subsequently reverted or moved
    # out of the final tree. Merge commits include each parent comparison.
    commits = _git(repo, "rev-list", f"{comparison}..{head}").decode("ascii").split()
    for commit in commits:
        if not COMMIT.fullmatch(commit):
            raise ComparisonUnavailable("incomplete_comparison")
        parents = _git(repo, "show", "-s", "--format=%P", commit).decode("ascii").split()
        if not parents:
            for status, path in _path_statuses(_git(
                repo, "diff-tree", "--no-ext-diff", "--no-textconv", "--no-renames",
                "--no-commit-id", "--root", "-r", "--name-status", "-z", commit, "--",
            )):
                changes.append((None, commit, status, path))
        else:
            for parent in parents:
                if not COMMIT.fullmatch(parent):
                    raise ComparisonUnavailable("incomplete_comparison")
                for status, path in _path_statuses(_git(
                    repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                    "--name-status", "-z", parent, commit, "--",
                )):
                    changes.append((parent, commit, status, path))
    return _classify_changes(repo, changes)


def select_timing(repo: Path, environment: dict[str, str], *, force_full: bool = False) -> dict:
    """Return categorical evidence; any uncertainty retains required full proof."""
    selection = {"mode": "full", "reason": "unavailable_comparison", "tested_commit": None}
    try:
        selection["tested_commit"] = _commit(repo, "HEAD")
        if force_full:
            selection["reason"] = "forced_full"
        elif environment.get("GITHUB_EVENT_NAME") != "pull_request":
            selection["reason"] = "non_pull_request"
        else:
            selection["reason"] = _pr_comparison(repo, environment, selection)
            if selection["reason"] == "unrelated_changes":
                selection["mode"] = "fast"
    except ComparisonUnavailable as error:
        selection["reason"] = error.reason
    return selection


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select", help="Conservatively select discovery's real-time proof")
    select.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    select.add_argument("--output", type=Path, required=True)
    select.add_argument("--github-output", type=Path)
    select.add_argument("--force-full", action="store_true")
    args = parser.parse_args()
    selection = select_timing(args.repo, dict(os.environ), force_full=args.force_full)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(selection, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"discovery_timing={selection['mode']}\n")
    print(json.dumps(selection, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
