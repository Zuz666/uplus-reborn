#!/usr/bin/env python3
"""Inspect a GitHub release candidate; publish only with explicit approval."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from delivery import build_artifact, load_artifact
from release import ASSET, VERSION, release_metadata

ASSETS = (ASSET, "SHA256SUMS", "release.json")


def gh(*args: str, payload: dict | None = None) -> str:
    command = ["gh", *args]
    if payload is not None:
        command.extend(["--method", "POST", "--input", "-"])
    result = subprocess.run(
        command, input=json.dumps(payload) if payload is not None else None,
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise ValueError(f"GitHub operation failed; inspect state before retrying: {result.stderr.strip()}")
    return result.stdout


def api(path: str, *, payload: dict | None = None) -> dict | list:
    return json.loads(gh("api", path, payload=payload))


def pages(path: str) -> list:
    return [item for page in json.loads(gh("api", "--paginate", "--slurp", path)) for item in page]


def validate_remote_state(metadata: dict, branch: dict, tags: list, releases: list, checks: list) -> None:
    """Reject stale candidates, missing gates, duplicate tags and version rollback."""
    if branch.get("name") != "master" or branch.get("commit", {}).get("sha") != metadata["commit"]:
        raise ValueError("Candidate is not the current public master commit")
    if not branch.get("protected"):
        raise ValueError("Release requires protected master")
    policy = branch.get("protection", {}).get("required_status_checks", {})
    required = set(policy.get("contexts", [])) | {c["context"] for c in policy.get("checks", [])}
    if "CI" not in required:
        raise ValueError("Protected master must require the aggregate CI gate")
    for context in required:
        app_ids = {c["app_id"] for c in policy.get("checks", []) if c["context"] == context and c.get("app_id", -1) != -1}
        if not any(
            check.get("name") == context and check.get("conclusion") == "success"
            and check.get("status") == "completed" and check.get("head_sha") == metadata["commit"]
            and (not app_ids or check.get("app", {}).get("id") in app_ids)
            for check in checks
        ):
            raise ValueError(f"Required check is not successful for this SHA/application: {context}")
    version = tuple(map(int, metadata["version"].split(".")))
    for name in {tag["name"] for tag in tags} | {release["tag_name"] for release in releases}:
        if name == metadata["tag"]:
            raise ValueError("Tag or release already exists; inspect it, never overwrite or blindly retry")
        if name.startswith("v"):
            if not VERSION.fullmatch(name[1:]):
                raise ValueError(f"Ambiguous existing version tag: {name}")
            if tuple(map(int, name[1:].split("."))) >= version:
                raise ValueError("Release version must be newer than every existing version tag/release")


def verify_candidate(repo: Path, directory: Path, metadata: dict) -> dict:
    """Bind uploaded bytes to the validated committed package and release notes."""
    artifact = load_artifact(directory / "bundle")
    with tempfile.TemporaryDirectory(prefix="uplus-release-source-") as temporary:
        committed = build_artifact(repo, metadata["commit"], Path(temporary) / "bundle")
    if artifact["files"] != committed["files"]:
        raise ValueError("Candidate package differs from the actual committed source")
    provenance = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    members = {name.removeprefix("custom_components/uplus_reborn/"): digest for name, digest in artifact["files"].items()}
    digest = hashlib.sha256((directory / ASSET).read_bytes()).hexdigest()
    expected = {
        "schema": 1, "commit": metadata["commit"], "version": metadata["version"],
        "asset": ASSET, "sha256": digest, "files": dict(sorted(members.items())),
    }
    if artifact["commit"] != metadata["commit"] or provenance != expected:
        raise ValueError("Candidate provenance does not match the selected commit/package")
    if (directory / "SHA256SUMS").read_text(encoding="utf-8") != f"{digest}  {ASSET}\n":
        raise ValueError("Candidate checksum file differs")
    if (directory / "release-notes.md").read_text(encoding="utf-8") != metadata["notes"]:
        raise ValueError("Candidate release notes differ from the committed changelog")
    with zipfile.ZipFile(directory / ASSET) as archive:
        names = archive.namelist()
        if len(names) != len(members) or set(names) != set(members):
            raise ValueError("ZIP has duplicate, unexpected or missing members")
        for name, expected_digest in members.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected_digest:
                raise ValueError(f"ZIP member differs from the committed package: {name}")
    return provenance


def tag_commit(repository: str, tag: str) -> str:
    target = api(f"repos/{repository}/git/ref/tags/{tag}")["object"]
    seen = set()
    while target["type"] == "tag":
        if target["sha"] in seen or len(seen) >= 8:
            raise ValueError("Cyclic or excessive annotated tag chain")
        seen.add(target["sha"])
        target = api(f"repos/{repository}/git/tags/{target['sha']}")["object"]
    if target["type"] != "commit":
        raise ValueError("Release tag does not resolve to a commit")
    return target["sha"]


def publish(repo: Path, repository: str, metadata: dict, directory: Path, *, approve: bool) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected owner/repository")
    provenance = verify_candidate(repo, directory, metadata)
    repository_info = api(f"repos/{repository}")
    if repository_info.get("default_branch") != "master" or repository_info.get("private"):
        raise ValueError("HACS publication requires a public repository with default master")
    branch = api(f"repos/{repository}/branches/master")
    tags = pages(f"repos/{repository}/tags?per_page=100")
    releases = pages(f"repos/{repository}/releases?per_page=100")
    check_pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repository}/commits/{metadata['commit']}/check-runs?filter=latest&per_page=100"))
    checks = [check for page in check_pages for check in page["check_runs"]]
    validate_remote_state(metadata, branch, tags, releases, checks)
    result = {"repository": repository, "commit": metadata["commit"], "tag": metadata["tag"], "sha256": provenance["sha256"], "published": False}
    if not approve:
        return result
    # No retries or automatic rollback after the first write: an uncertain outcome
    # requires inspection of the exact tag/draft/assets before a maintainer resumes.
    api(f"repos/{repository}/git/refs", payload={"ref": f"refs/tags/{metadata['tag']}", "sha": metadata["commit"]})
    if tag_commit(repository, metadata["tag"]) != metadata["commit"]:
        raise ValueError("Created tag does not resolve to the approved commit")
    gh("release", "create", metadata["tag"], "--repo", repository, "--verify-tag", "--target", metadata["commit"], "--title", f"U+ Reborn {metadata['version']}", "--notes-file", str(directory / "release-notes.md"), "--draft")
    gh("release", "upload", metadata["tag"], *[str(directory / name) for name in ASSETS], "--repo", repository)
    with tempfile.TemporaryDirectory(prefix="uplus-release-download-") as temporary:
        gh("release", "download", metadata["tag"], "--repo", repository, "--dir", temporary)
        for name in ASSETS:
            uploaded = hashlib.sha256((Path(temporary) / name).read_bytes()).digest()
            if uploaded != hashlib.sha256((directory / name).read_bytes()).digest():
                raise ValueError(f"Uploaded asset differs: {name}; draft must not be published")
    # master may have advanced while assets were uploaded; do not publish stale work.
    if api(f"repos/{repository}/branches/master")["commit"]["sha"] != metadata["commit"]:
        raise ValueError("Master advanced during draft preparation; inspect the draft before resuming")
    gh("release", "edit", metadata["tag"], "--repo", repository, "--draft=false", "--latest")
    released = api(f"repos/{repository}/releases/tags/{metadata['tag']}")
    if released["draft"] or tag_commit(repository, metadata["tag"]) != metadata["commit"]:
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
    parser.add_argument("--approve", action="store_true", help="Explicit owner-approved tag/draft/asset/publication writes")
    args = parser.parse_args(argv)
    try:
        metadata = release_metadata(args.repo, args.commit, args.version)
        print(json.dumps(publish(args.repo, args.repository, metadata, args.candidate, approve=args.approve), indent=2, sort_keys=True))
        return 0
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile) as error:
        print(f"publish_release: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
