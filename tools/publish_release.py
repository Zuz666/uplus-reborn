#!/usr/bin/env python3
"""Inspect a GitHub release candidate; publish only with explicit approval."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import github_api as github
import release_evidence
from release import ASSET, VERSION, release_metadata

ASSETS = (ASSET, "SHA256SUMS", "release.json")


def validate_remote_state(metadata: dict, branch: dict, tags: list, releases: list, checks: list) -> None:
    """Reject stale candidates, missing gates, duplicate tags and version rollback."""
    release_evidence.validate_protected_branch(metadata, branch, checks)
    version = tuple(map(int, metadata["version"].split(".")))
    for name in {tag["name"] for tag in tags} | {release["tag_name"] for release in releases}:
        if name == metadata["tag"]:
            raise ValueError("Tag or release already exists; inspect it, never overwrite or blindly retry")
        if name.startswith("v"):
            if not VERSION.fullmatch(name[1:]):
                raise ValueError(f"Ambiguous existing version tag: {name}")
            if tuple(map(int, name[1:].split("."))) >= version:
                raise ValueError("Release version must be newer than every existing version tag/release")


def publish(repo: Path, repository: str, metadata: dict, directory: Path, *,
            selection: Path, matrix: Path, approve: bool) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected owner/repository")
    tags = github.pages(f"repos/{repository}/tags?per_page=100")
    releases = github.pages(f"repos/{repository}/releases?per_page=100")
    proof = release_evidence.verify_source_evidence(repo, repository, metadata, directory, matrix, selection)
    source = proof["selection"]
    validate_remote_state(metadata, source["branch"], tags, releases, source["checks"])
    result = {"repository": repository, "commit": metadata["commit"], "tag": metadata["tag"],
              "sha256": proof["candidate"]["sha256"], "source_run": source["source"], "published": False}
    if not approve:
        return result
    # No retries or automatic rollback after the first write: an uncertain outcome
    # requires inspection of the exact tag/draft/assets before a maintainer resumes.
    github.api(f"repos/{repository}/git/refs", payload={"ref": f"refs/tags/{metadata['tag']}", "sha": metadata["commit"]})
    if github.tag_commit(repository, metadata["tag"]) != metadata["commit"]:
        raise ValueError("Created tag does not resolve to the approved commit")
    created = github.api(f"repos/{repository}/releases", payload={
        "tag_name": metadata["tag"], "target_commitish": metadata["commit"],
        "name": f"U+ Reborn {metadata['version']}",
        "body": (directory / "release-notes.md").read_text(encoding="utf-8"),
        "draft": True,
    })
    release_id = created["id"]
    if (type(release_id) is not int or release_id <= 0
            or created.get("tag_name") != metadata["tag"] or created.get("draft") is not True
            or created.get("prerelease") is not False):
        raise ValueError("Created draft identity differs; inspect actual state before resuming")
    github.gh("release", "upload", metadata["tag"], *[str(directory / name) for name in ASSETS], "--repo", repository)
    with tempfile.TemporaryDirectory(prefix="uplus-release-download-") as temporary:
        github.gh("release", "download", metadata["tag"], "--repo", repository, "--dir", temporary)
        for name in ASSETS:
            uploaded = hashlib.sha256((Path(temporary) / name).read_bytes()).digest()
            if uploaded != hashlib.sha256((directory / name).read_bytes()).digest():
                raise ValueError(f"Uploaded asset differs: {name}; draft must not be published")
    # Approval/upload can outlive master, protection, the selected CI attempt or
    # its artifacts. Recheck all of them without relaxing duplicate-tag guards.
    release_evidence.verify_source_evidence(repo, repository, metadata, directory, matrix, selection)
    draft = github.api(f"repos/{repository}/releases/{release_id}")
    if (draft.get("id") != release_id or draft.get("tag_name") != metadata["tag"]
            or draft.get("draft") is not True or draft.get("prerelease") is not False
            or {asset["name"] for asset in draft["assets"]} != set(ASSETS)
            or github.tag_commit(repository, metadata["tag"]) != metadata["commit"]):
        raise ValueError("Draft/tag/asset state changed; inspect actual state before resuming")
    github.gh("release", "edit", metadata["tag"], "--repo", repository, "--draft=false", "--latest")
    released = github.api(f"repos/{repository}/releases/tags/{metadata['tag']}")
    if (released.get("id") != release_id or released.get("tag_name") != metadata["tag"]
            or released.get("draft") is not False
            or github.tag_commit(repository, metadata["tag"]) != metadata["commit"]):
        raise ValueError("Published release/tag acceptance failed; inspect actual state")
    result.update(published=True, url=released["html_url"], immutable=released.get("immutable", False))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--approve", action="store_true", help="Explicit owner-approved tag/draft/asset/publication writes")
    args = parser.parse_args(argv)
    try:
        metadata = release_metadata(args.repo, args.commit, args.version)
        print(json.dumps(publish(args.repo, args.repository, metadata, args.candidate,
                                 selection=args.selection, matrix=args.matrix, approve=args.approve), indent=2, sort_keys=True))
        return 0
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile, github.GitHubOperationError) as error:
        print(f"publish_release: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
