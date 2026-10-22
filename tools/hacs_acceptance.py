"""Repeat actual published HACS installation/upgrade in disposable Docker Core.

No publication, account onboarding or live appliance access. The host needs GitHub
CLI read access; installer workers use anonymous HTTPS or an ephemeral Actions token.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import subprocess
import time
import uuid
import zipfile
from pathlib import Path

from delivery import (
    PACKAGE,
    _member_name,
    _no_links,
    _write_json,
    ensure_docker_image,
    require_docker,
)
from github_api import api, gh, tag_commit
from release import ASSET, VERSION

TOOLS = Path(__file__).resolve().parent
MAX_PACKAGE_BYTES = 16 * 1024 * 1024


def verify_release_package(directory: Path, released: dict, commit: str, tree: dict) -> dict:
    """Bind all downloaded members to the actual immutable tag's Git tree."""
    tag = released.get("tag_name", "")
    if not tag.startswith("v") or not VERSION.fullmatch(tag[1:]):
        raise ValueError("Expected a stable vMAJOR.MINOR.PATCH release")
    if released.get("draft") or released.get("prerelease") or released.get("immutable") is not True:
        raise ValueError("Acceptance requires a published immutable stable release")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or tree.get("truncated") is not False:
        raise ValueError("Missing complete committed source identity")
    expected = {}
    for row in tree["tree"]:
        if row["path"].startswith(PACKAGE + "/") and row["type"] != "tree":
            if row["type"] != "blob" or row.get("mode") not in ("100644", "100755"):
                raise ValueError("Unsafe committed source member")
            name = _member_name(row["path"])[len(PACKAGE) + 1:]
            expected[name] = row["sha"]
    archive_path = directory / ASSET
    if archive_path.stat().st_size > MAX_PACKAGE_BYTES:
        raise ValueError("Release ZIP is too large")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    provenance = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    files = provenance.get("files", {})
    if provenance != {"schema": 1, "commit": commit, "version": tag[1:], "asset": ASSET,
                      "sha256": digest, "files": files} or set(files) != set(expected):
        raise ValueError("Release provenance differs from committed source")
    if (directory / "SHA256SUMS").read_text(encoding="utf-8") != f"{digest}  {ASSET}\n":
        raise ValueError("Release checksum differs")
    with zipfile.ZipFile(archive_path) as archive:
        rows = archive.infolist()
        if len(rows) != len(expected) or {r.filename for r in rows} != set(expected):
            raise ValueError("Release ZIP has duplicate, unsafe, missing or extra members")
        if sum(r.file_size for r in rows) > MAX_PACKAGE_BYTES:
            raise ValueError("Unpacked release is too large")
        for row in rows:
            _member_name(PACKAGE + "/" + row.filename)
            if stat.S_ISLNK(row.external_attr >> 16) or row.flag_bits & 1:
                raise ValueError("Release ZIP contains a link or encrypted member")
            data = archive.read(row)
            blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data, usedforsecurity=False).hexdigest()
            if blob != expected[row.filename] or hashlib.sha256(data).hexdigest() != files[row.filename]:
                raise ValueError("Release member differs from committed source")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("domain") != "uplus_reborn" or manifest.get("version") != tag[1:] or "LICENSE" not in files:
            raise ValueError("Installed package domain/version/license differs")
    return {"schema": 1, "commit": commit, "version": tag[1:], "tag": tag, "zip_sha256": digest,
            "files": {PACKAGE + "/" + name: value for name, value in files.items()}}


def fetch_release(repository: str, tag: str, directory: Path) -> dict:
    released = api(f"repos/{repository}/releases/tags/{tag}")
    commit = tag_commit(repository, tag)
    tree = api(f"repos/{repository}/git/trees/{commit}?recursive=1")
    directory.mkdir()
    for name in (ASSET, "SHA256SUMS", "release.json"):
        gh("release", "download", tag, "--repo", repository, "--pattern", name, "--dir", str(directory))
    manifest = verify_release_package(directory, released, commit, tree)
    _write_json(directory / "expected.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="Zuz666/uplus-reborn")
    parser.add_argument("--tag", required=True)
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--baseline")
    baseline.add_argument("--first-release", action="store_true", help="Explicit fresh-install-only bootstrap")
    parser.add_argument("--core-target", choices=("core-current",), default="core-current")
    parser.add_argument("--docker-bin", default=os.environ.get("UPLUS_DOCKER_BIN", "docker"))
    parser.add_argument("--expect-latest", action="store_true")
    parser.add_argument("--output", type=Path, required=True, help="New, unoccupied output directory")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("Expected owner/repository")
    for tag in (args.tag, args.baseline):
        if tag is not None and (not tag.startswith("v") or not VERSION.fullmatch(tag[1:])):
            parser.error("Expected stable vMAJOR.MINOR.PATCH tags")
    if args.baseline and tuple(map(int, args.baseline[1:].split("."))) >= tuple(map(int, args.tag[1:].split("."))):
        parser.error("Baseline must precede the target release")
    output = args.output.absolute()
    _no_links(output)
    if output.exists():
        parser.error("Output directory is occupied; retain previous evidence")
    started = time.monotonic()
    result = {"schema": 1, "scenario": "published-hacs", "status": "running", "success": False,
              "repository": args.repository, "tag": args.tag, "baseline": args.baseline,
              "account_onboarding": False, "live_appliance_access": False}
    result["python_version"] = platform.python_version()
    result["tool_hashes"] = {
        name: hashlib.sha256((TOOLS / name).read_bytes()).hexdigest()
        for name in ("hacs_acceptance.py", "hacs_fixture.py", "hacs-fixture.json",
                     "upgrade_smoke.py", "ha_smoke.py", "delivery.py", "github_api.py", "release.py")
    }
    name = "uplus-hacs-" + uuid.uuid4().hex
    owns_output = False
    try:
        output.mkdir(parents=True)
        owns_output = True
        _write_json(output / "acceptance.json", result)
        require_docker(args.docker_bin)
        inputs, receipts = output / "inputs", output / "receipts"
        inputs.mkdir()
        receipts.mkdir()
        fixture = json.loads((TOOLS / "hacs-fixture.json").read_text(encoding="utf-8"))
        gh("release", "download", fixture["hacs_version"], "--repo", "hacs/integration",
           "--pattern", "hacs.zip", "--dir", str(inputs))
        if hashlib.sha256((inputs / "hacs.zip").read_bytes()).hexdigest() != fixture["hacs_sha256"]:
            raise ValueError("Official HACS archive digest differs from the reviewed fixture")
        _write_json(inputs / "fixture.json", fixture)
        result["target"] = fetch_release(args.repository, args.tag, inputs / "target")
        if args.baseline:
            result["previous"] = fetch_release(args.repository, args.baseline, inputs / "baseline")
        if args.expect_latest and api(f"repos/{args.repository}/releases/latest")["tag_name"] != args.tag:
            raise ValueError("Target is not the latest stable release")
        image = ensure_docker_image(TOOLS.parent, args.core_target, docker_bin=args.docker_bin)
        result["environment"] = image
        command = [args.docker_bin, "run", "--rm", "--init", "--name", name, "--network", "bridge",
                   "--mount", f"type=bind,src={TOOLS.parent},dst=/workspace,readonly",
                   "--mount", f"type=bind,src={inputs},dst=/inputs,readonly",
                   "--mount", f"type=bind,src={receipts},dst=/evidence", "--workdir", "/tmp"]
        # Only the repository-scoped ephemeral CI token may cross into the installer.
        if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GH_TOKEN"):
            command.extend(["--env", "GH_TOKEN"])
        command.extend(["--entrypoint", "python3", image["image"], "/workspace/tools/hacs_fixture.py",
                        "--repository", args.repository, "--tag", args.tag])
        if args.baseline:
            command.extend(["--baseline", args.baseline])
        if args.expect_latest:
            command.append("--expect-latest")
        completed = subprocess.run(command, capture_output=True, text=True, timeout=1200, check=False)
        receipt_path = receipts / "result.json"
        if receipt_path.exists():
            result["fixture"] = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (completed.returncode != 0 or not result.get("fixture", {}).get("success")
                or not result["fixture"].get("temporary_configs_removed")):
            raise RuntimeError("HACS acceptance failed; inspect the sanitized phase receipts")
        result.update(success=True, status="accepted")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        result.update(status="blocked" if isinstance(error, RuntimeError) else "failed",
                      error=f"{type(error).__name__}: {error}".split("?", 1)[0][:1000])
        if isinstance(error, subprocess.TimeoutExpired):
            try:
                cleanup = subprocess.run([args.docker_bin, "rm", "--force", name], capture_output=True, check=False, timeout=45)
                result["timeout_container_removed"] = cleanup.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                result["timeout_container_removed"] = False
    finally:
        result["duration_seconds"] = time.monotonic() - started
        if owns_output:
            _write_json(output / "acceptance.json", result)
    print(json.dumps({key: result[key] for key in ("schema", "status", "success", "duration_seconds", "error") if key in result}))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
