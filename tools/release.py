#!/usr/bin/env python3
"""Validate release metadata and package exact committed bytes for HACS."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from datetime import date
from pathlib import Path

from delivery import (
    PACKAGE,
    build_artifact,
    committed_package_files,
    extract_artifact,
    load_artifact,
)

VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
ASSET = "uplus_reborn.zip"


def previous_stable(releases: list[dict], version: str) -> str | None:
    """Freeze the greatest published stable version below the new target."""
    if not VERSION.fullmatch(version):
        raise ValueError("Expected a stable release version")
    target = tuple(map(int, version.split(".")))
    chosen = None
    chosen_version = (-1, -1, -1)
    for released in releases:
        tag = released.get("tag_name", "")
        if released.get("draft") or released.get("prerelease") or not tag.startswith("v") or not VERSION.fullmatch(tag[1:]):
            continue
        candidate = tuple(map(int, tag[1:].split(".")))
        if chosen_version < candidate < target:
            chosen, chosen_version = tag, candidate
    return chosen


def git(repo: Path, *args: str) -> bytes:
    """Read committed release metadata without depending on checkout state."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False,
    )
    if result.returncode:
        raise ValueError(f"git {args[0]} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def release_metadata(repo: Path, commit: str, expected: str | None = None) -> dict:
    """Require one stable version and a dated, nonempty release-note section."""
    resolved = git(repo, "rev-parse", "--verify", "--end-of-options", commit + "^{commit}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise ValueError("Expected a full SHA-1 commit")

    def text(name: str) -> str:
        return git(repo, "show", f"{resolved}:{name}").decode("utf-8")

    manifest = json.loads(text(f"{PACKAGE}/manifest.json"))
    project = tomllib.loads(text("pyproject.toml"))["project"]
    lock = tomllib.loads(text("uv.lock"))
    version = manifest.get("version", "")
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError("Release version must be stable MAJOR.MINOR.PATCH without leading zeros")
    if manifest.get("domain") != "uplus_reborn" or project.get("name") != "uplus-reborn":
        raise ValueError("Unexpected integration/package identity")
    roots = [p for p in lock.get("package", []) if p.get("name") == "uplus-reborn"]
    if project.get("version") != version or len(roots) != 1 or roots[0].get("version") != version:
        raise ValueError("Manifest, project and uv.lock root versions differ")
    if expected is not None and expected != version:
        raise ValueError("Requested version differs from committed metadata")
    changelog = text("CHANGELOG.md")
    headings = list(re.finditer(
        r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})[ \t]*$",
        changelog, re.MULTILINE,
    ))
    matching = [m for m in headings if m.group(1) == version]
    if len(matching) != 1 or matching[0] != headings[0]:
        raise ValueError("Current version needs one dated, first release section in CHANGELOG.md")
    heading = matching[0]
    date.fromisoformat(heading.group(2))
    following = re.search(r"^## ", changelog[heading.end():], re.MULTILINE)
    end = heading.end() + following.start() if following else len(changelog)
    notes = changelog[heading.end():end].strip()
    if not notes or not re.search(r"^[-*] \S", notes, re.MULTILINE):
        raise ValueError("Release notes must describe user-visible changes")
    license_text = text(f"{PACKAGE}/LICENSE")
    if "MIT License" not in license_text or "Permission is hereby granted" not in license_text:
        raise ValueError("The shipped integration must contain its MIT notice")
    return {
        "schema": 1, "commit": resolved, "version": version,
        "tag": f"v{version}", "date": heading.group(2), "notes": notes + "\n",
    }


def verify_candidate(repo: Path, directory: Path, metadata: dict) -> dict:
    """Bind uploaded bytes to the validated committed package and release notes."""
    artifact = load_artifact(directory / "bundle")
    if artifact["files"] != committed_package_files(repo, metadata["commit"]):
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


def build_release(repo: Path, commit: str, output: Path, expected: str | None = None) -> dict:
    """Build a deterministic root-layout ZIP after the existing archive safety checks."""
    metadata = release_metadata(repo, commit, expected)
    output = output.absolute()
    for path in (output, *output.parents):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Release output must not traverse a link")
    output.mkdir(parents=True, exist_ok=False)
    try:
        artifact = build_artifact(repo, metadata["commit"], output / "bundle")
        with tempfile.TemporaryDirectory(prefix="uplus-release-") as temporary:
            root = Path(temporary)
            extract_artifact(output / "bundle", root)
            members = {
                name.removeprefix(PACKAGE + "/"): digest
                for name, digest in artifact["files"].items()
            }
            required = {"__init__.py", "manifest.json", "LICENSE", "translations/en.json"}
            if not required.issubset(members):
                raise ValueError("Incomplete integration package")
            # Stored entries avoid compressor-version differences across platforms.
            with zipfile.ZipFile(output / ASSET, "x", compression=zipfile.ZIP_STORED) as archive:
                for name in sorted(members):
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    info.compress_type = zipfile.ZIP_STORED
                    archive.writestr(info, (root / PACKAGE / name).read_bytes())
        digest = hashlib.sha256((output / ASSET).read_bytes()).hexdigest()
        provenance = {
            "schema": 1, "commit": metadata["commit"], "version": metadata["version"],
            "asset": ASSET, "sha256": digest, "files": dict(sorted(members.items())),
        }
        (output / "release.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        (output / "SHA256SUMS").write_text(f"{digest}  {ASSET}\n", encoding="utf-8", newline="\n")
        (output / "release-notes.md").write_text(metadata["notes"], encoding="utf-8", newline="\n")
        return provenance
    except BaseException:
        shutil.rmtree(output)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("check", "build"):
        command = commands.add_parser(action)
        command.add_argument("--repo", type=Path, default=Path.cwd())
        command.add_argument("--commit", required=True)
        command.add_argument("--version")
        if action == "build":
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "build":
            result = build_release(args.repo, args.commit, args.output, args.version)
        else:
            result = release_metadata(args.repo, args.commit, args.version)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"release: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
