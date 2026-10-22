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
from datetime import UTC, date, datetime
from pathlib import Path

import github_api as github
from delivery import (
    PACKAGE,
    _no_links,
    append_github_output,
    build_artifact,
    committed_package_files,
    extract_artifact,
    load_artifact,
    replace_toml_version,
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


def baseline_artifact(repo: Path, repository: str, tag: str, output: Path) -> dict:
    """Fetch one actual public stable tag without moving a checkout/branch."""
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not tag.startswith("v") or not VERSION.fullmatch(tag[1:])):
        raise ValueError("Expected public owner/repository and a stable vMAJOR.MINOR.PATCH baseline")
    output = output.absolute()
    _no_links(output)
    if output.exists():
        raise FileExistsError("Baseline output already exists; use a new directory")
    info = github.api(f"repos/{repository}")
    if info.get("private") is not False or info.get("full_name") != repository:
        raise ValueError("Baseline must belong to the exact public repository")
    commit = github.tag_commit(repository, tag)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Baseline tag must resolve to a full Git commit")
    with tempfile.TemporaryDirectory(prefix="uplus-release-baseline-") as temporary:
        source = Path(temporary) / "repository"
        git(repo, "init", "--bare", "--object-format=sha1", "--initial-branch=master", str(source))
        fetched = subprocess.run(
            ["git", "-C", str(source), "fetch", "--no-tags", "--depth=1", f"https://github.com/{repository}.git", commit],
            capture_output=True, check=False, timeout=120,
        )
        if fetched.returncode:
            raise ValueError(f"Cannot fetch public baseline {tag} at {commit}; inspect Git/network access")
        release_metadata(source, commit, tag[1:])
        if github.tag_commit(repository, tag) != commit:
            raise ValueError("Baseline tag changed during fetch")
        artifact = build_artifact(source, commit, output)
    return {"schema": 1, "scenario": "baseline-artifact", "repository": repository,
            "tag": tag, "commit": commit, "artifact": artifact, "temporary_repo_removed": not source.exists()}


def prepare_release(repo: Path, version: str, released: str, notes_path: Path) -> dict:
    """Prepare only release metadata; never commit, resolve dependencies or publish."""
    current = release_metadata(repo, "HEAD")
    if not VERSION.fullmatch(version) or tuple(map(int, version.split("."))) <= tuple(map(int, current["version"].split("."))):
        raise ValueError("Preparation requires a strictly newer stable version")
    date.fromisoformat(released)
    names = (f"{PACKAGE}/manifest.json", "pyproject.toml", "uv.lock", "CHANGELOG.md")
    if git(repo, "diff", "--name-only", "HEAD", "--", *names).strip():
        raise ValueError("Release metadata has owner changes; preserve and review them before preparation")
    for path in [notes_path.absolute(), *(repo / name for name in names)]:
        _no_links(path.absolute())
    notes = notes_path.read_text(encoding="utf-8").strip()
    if not re.search(r"^[-*] \S", notes, re.MULTILINE) or re.search(r"^## \[", notes, re.MULTILINE):
        raise ValueError("Supply curated bullet notes without a release heading")
    originals = {name: (repo / name).read_bytes() for name in names}
    texts = {name: value.decode("utf-8").replace("\r\n", "\n") for name, value in originals.items()}
    manifest = json.loads(texts[names[0]])
    manifest["version"] = version
    project = tomllib.loads(texts["pyproject.toml"])
    project["project"]["version"] = version
    lock = tomllib.loads(texts["uv.lock"])
    root = next(p for p in lock["package"] if p["name"] == "uplus-reborn")
    if root.get("source") != {"virtual": "."}:
        raise ValueError("Only the existing non-installed virtual root can be updated without resolution")
    root["version"] = version
    heading = re.search(r"^## \[", texts["CHANGELOG.md"], re.MULTILINE)
    if heading is None:
        raise ValueError("Missing current changelog boundary")
    changed = {
        names[0]: (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(),
        "pyproject.toml": replace_toml_version(texts["pyproject.toml"], current["version"], version, project),
        "uv.lock": replace_toml_version(texts["uv.lock"], current["version"], version, lock),
        "CHANGELOG.md": (texts["CHANGELOG.md"][:heading.start()] + f"## [{version}] - {released}\n\n"
                         + notes + "\n\n" + texts["CHANGELOG.md"][heading.start():]).encode(),
    }
    written = []
    try:
        for name, value in changed.items():
            if (repo / name).read_bytes() != originals[name]:
                raise ValueError("Metadata changed concurrently; inspect actual files")
            (repo / name).write_bytes(value)
            written.append(name)
    except (OSError, ValueError):
        for name in written:
            if (repo / name).read_bytes() == changed[name]:
                (repo / name).write_bytes(originals[name])
        raise
    return {"schema": 1, "status": "prepared_not_committed", "previous_version": current["version"],
            "version": version, "files": list(changed), "dependency_resolution": "unchanged",
            "publication": "not_attempted"}


def validated_run_status(info: dict, workflow: dict, run: dict, jobs: list[dict], commit: str) -> dict:
    """A direct run/attempt observation, not release authorization or search indexing."""
    if (info.get("private") is not False or info.get("default_branch") != "master"
            or run.get("repository", {}).get("id") != info["id"]
            or run.get("head_repository", {}).get("id") != info["id"]
            or run.get("head_sha") != commit or run.get("head_branch") != "master"
            or run.get("workflow_id") != workflow["id"] or run.get("path") != workflow["path"]
            or type(run.get("run_attempt")) is not int or run["run_attempt"] < 1):
        raise ValueError("Run identity differs from the exact repository/master/SHA/workflow")
    if any(j.get("run_id") != run["id"] or j.get("run_attempt") != run["run_attempt"] for j in jobs):
        raise ValueError("Job belongs to another run or attempt")
    if run["status"] != "completed":
        state = "pending"
    elif not jobs or any(j.get("status") != "completed" for j in jobs):
        state = "inconsistent"
    elif run.get("conclusion") != "success" or any(j.get("conclusion") not in {"success", "skipped", "neutral"} for j in jobs):
        state = "failed"
    else:
        state = "completed"
    return {"schema": 1, "state": state, "publication_eligibility": "not_evaluated",
            "repository": info["full_name"], "commit": commit, "workflow": workflow["path"],
            "run": {key: run[key] for key in ("id", "run_attempt", "status", "conclusion", "html_url")},
            "jobs": [{key: row.get(key) for key in ("id", "name", "run_id", "run_attempt", "status", "conclusion")}
                     for row in jobs], "observed_at": datetime.now(UTC).isoformat()}


def run_status(repository: str, run_id: int, commit: str, workflow_name: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[0-9a-f]{40}", commit) or run_id < 1:
        raise ValueError("Expected public repository, full SHA and positive run ID")
    info = github.api(f"repos/{repository}")
    workflow = github.api(f"repos/{repository}/actions/workflows/{workflow_name}.yml")
    run = github.api(f"repos/{repository}/actions/runs/{run_id}")
    if run.get("id") != run_id or info.get("full_name") != repository:
        raise ValueError("Backend run/repository identity differs")
    jobs = github.pages(f"repos/{repository}/actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs?per_page=100", key="jobs")
    fresh = github.api(f"repos/{repository}/actions/runs/{run_id}")
    if fresh.get("run_attempt") != run["run_attempt"]:
        raise ValueError("Attempt changed during read; observe this exact run again without repeating writes")
    return validated_run_status(info, workflow, fresh, jobs, commit)


def advisory_delta(current: dict, baseline: dict | None) -> dict:
    if baseline is None:
        return {"mode": "snapshot_only", "new": None, "changed": None, "no_longer_open": None}
    if (baseline.get("schema") != 1 or baseline.get("status") != "observed"
            or baseline.get("repository") != current["repository"]
            or baseline.get("repository_id") != current["repository_id"]):
        raise ValueError("Advisory baseline must be an observed inventory for this repository")
    old, new = ({row["number"]: row for row in value["alerts"]} for value in (baseline, current))
    return {"mode": "compared", "new": sorted(new.keys() - old.keys()),
            "changed": sorted(key for key in new.keys() & old.keys() if new[key] != old[key]),
            "no_longer_open": sorted(old.keys() - new.keys())}


def advisory_inventory(repository: str, baseline: Path | None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected public owner/repository")
    info = github.api(f"repos/{repository}")
    if info.get("private") is not False or info.get("full_name") != repository:
        raise ValueError("Expected the exact public repository")
    result = {"schema": 1, "repository": repository, "repository_id": info["id"],
              "observed_at": datetime.now(UTC).isoformat()}
    try:
        rows = github.pages(f"repos/{repository}/dependabot/alerts?state=open&per_page=100")
    except github.GitHubOperationError as error:
        return {**result, "status": "unavailable", "error": str(error), "delta": None}
    alerts = []
    for row in rows:
        vulnerability = row["security_vulnerability"]
        alerts.append({"number": row["number"], "ghsa": row["security_advisory"]["ghsa_id"],
                       "severity": vulnerability["severity"], "package": vulnerability["package"],
                       "manifest": row["dependency"]["manifest_path"],
                       "range": vulnerability["vulnerable_version_range"],
                       "first_patched_version": vulnerability["first_patched_version"]})
    result.update(status="observed", alerts=sorted(alerts, key=lambda row: row["number"]))
    if baseline is not None:
        _no_links(baseline.absolute())
    result["delta"] = advisory_delta(result, json.loads(baseline.read_text(encoding="utf-8")) if baseline else None)
    return result


def release_outcome(requested: bool, inspection: str, publication: str, acceptance: str) -> str:
    if not requested:
        if publication not in {"skipped", "not_requested", "unknown"}:
            return "publication_unknown_inspect_before_writes"
        return "audited_publication_not_requested" if inspection == "success" else "audit_blocked"
    if inspection != "success" and publication == "skipped":
        return "publication_not_attempted"
    if publication != "success":
        return "publication_unknown_inspect_before_writes"
    return "published_and_accepted" if acceptance == "success" else "published_acceptance_blocked"


def release_report(repository: str, run_id: int, commit: str, version: str, requested: bool,
                   source_run: int | None, advisories: Path | None) -> dict:
    if not VERSION.fullmatch(version):
        raise ValueError("Expected stable version")
    observed = run_status(repository, run_id, commit, "release")
    def phase(prefix: str) -> str:
        rows = [row for row in observed["jobs"] if row["name"] == prefix or row["name"].startswith(prefix + " / ")]
        if not rows:
            return "unknown"
        if any(row["status"] != "completed" for row in rows):
            return "pending"
        if all(row["conclusion"] == "skipped" for row in rows):
            return "skipped"
        return "success" if all(row["conclusion"] == "success" for row in rows) else "failure"
    phases = {"inspection": phase("Inspect accepted master evidence"), "publication": phase("publish"),
              "hacs": phase("hacs")}
    if not requested:
        for name in ("publication", "hacs"):
            if phases[name] in {"unknown", "skipped"}:
                phases[name] = "not_requested"
    state = release_outcome(requested, *phases.values())
    releases = github.pages(f"repos/{repository}/releases?per_page=100")
    published = next((row for row in releases if row["tag_name"] == "v" + version and not row["draft"]), None)
    release = None if published is None else {
        "url": published["html_url"], "tag": published["tag_name"],
        "commit": github.tag_commit(repository, published["tag_name"]),
        "immutable": published.get("immutable"), "published_at": published["published_at"],
    }
    if state.startswith("published_") and (release is None or release["commit"] != commit):
        state = "publication_unknown_inspect_before_writes"
    artifacts = github.pages(f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100", key="artifacts")
    if state == "published_and_accepted" and not any(
            row["name"].startswith("hacs-acceptance-") and not row.get("expired", True) for row in artifacts):
        state = "published_acceptance_blocked"
    limits = ["No live HA/appliance or hardware acceptance.",
              "Read-only audit does not assert publication eligibility.",
              "Artifact names are references, not independent byte verification."]
    inventory = None
    if advisories is not None:
        _no_links(advisories.absolute())
        inventory = json.loads(advisories.read_text(encoding="utf-8"))
        if inventory.get("repository") != repository:
            raise ValueError("Advisory inventory belongs to another repository")
    if inventory is None or inventory.get("status") != "observed":
        limits.append("Advisory inventory unavailable; no zero-risk or no-new-advisory claim.")
    return {"schema": 1, "state": state, "version": version, "source": observed,
            "source_ci_run": source_run, "publication_requested": requested, "phases": phases,
            "release": release, "hacs": {"status": phases["hacs"], "evidence": [
                row["id"] for row in artifacts if row["name"].startswith("hacs-acceptance-")]},
            "evidence": [{key: row.get(key) for key in ("id", "name", "digest", "expired", "expires_at")}
                         for row in artifacts], "advisories": inventory, "limits": limits}


def save_result(result: dict, output: Path | None, summary: Path | None = None) -> None:
    if output is not None:
        _no_links(output.absolute())
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, indent=2, sort_keys=True)
            stream.write("\n")
    if summary is not None:
        _no_links(summary.absolute())
        with summary.open("a", encoding="utf-8", newline="\n") as stream:
            concise = {key: result.get(key) for key in ("state", "version", "source_ci_run", "phases", "release", "hacs", "limits")}
            stream.write("## Release outcome\n\n```json\n" + json.dumps(concise, indent=2) + "\n```\n")


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
    baseline = commands.add_parser("baseline")
    baseline.add_argument("--repo", type=Path, default=Path.cwd())
    baseline.add_argument("--repository", required=True)
    baseline.add_argument("--tag", required=True)
    baseline.add_argument("--output", type=Path, required=True)
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--repo", type=Path, default=Path.cwd())
    preparation.add_argument("--version", required=True)
    preparation.add_argument("--date", required=True)
    preparation.add_argument("--notes", type=Path, required=True)
    selector = commands.add_parser("baseline-select")
    selector.add_argument("--repository", required=True)
    selector.add_argument("--version", required=True)
    selector.add_argument("--github-output", type=Path)
    selector.add_argument("--output", type=Path)
    status = commands.add_parser("status")
    report = commands.add_parser("report")
    for command in (status, report):
        command.add_argument("--repository", required=True)
        command.add_argument("--run-id", type=int, required=True)
        command.add_argument("--commit", required=True)
        command.add_argument("--output", type=Path)
    status.add_argument("--workflow", choices=("ci", "release", "hacs-acceptance"), required=True)
    report.add_argument("--version", required=True)
    report.add_argument("--publication-requested", choices=("true", "false"), required=True)
    report.add_argument("--source-run", type=int)
    report.add_argument("--advisories", type=Path)
    report.add_argument("--summary", type=Path)
    advisory = commands.add_parser("advisories")
    advisory.add_argument("--repository", required=True)
    advisory.add_argument("--baseline", type=Path)
    advisory.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "build":
            result = build_release(args.repo, args.commit, args.output, args.version)
        elif args.action == "baseline":
            result = baseline_artifact(args.repo, args.repository, args.tag, args.output)
        elif args.action == "prepare":
            result = prepare_release(args.repo, args.version, args.date, args.notes)
        elif args.action == "baseline-select":
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
                raise ValueError("Expected public owner/repository")
            tag = previous_stable(github.pages(f"repos/{args.repository}/releases?per_page=100"), args.version)
            result = {"schema": 1, "repository": args.repository, "version": args.version,
                      "tag": tag, "first_release": tag is None}
            if args.github_output is not None:
                append_github_output(args.github_output, {"tag": tag or "", "first_release": str(tag is None).lower()})
            save_result(result, args.output)
        elif args.action == "status":
            result = run_status(args.repository, args.run_id, args.commit, args.workflow)
            save_result(result, args.output)
        elif args.action == "advisories":
            result = advisory_inventory(args.repository, args.baseline)
            save_result(result, args.output)
        elif args.action == "report":
            result = release_report(args.repository, args.run_id, args.commit, args.version,
                                    args.publication_requested == "true", args.source_run, args.advisories)
            save_result(result, args.output, args.summary)
        else:
            result = release_metadata(args.repo, args.commit, args.version)
        print(json.dumps(result, indent=2, sort_keys=True))
        if args.action == "advisories" and result["status"] != "observed":
            return 2
        if args.action == "status" and result["state"] != "completed":
            return 2
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, github.GitHubOperationError) as error:
        print(f"release: {error}", file=sys.stderr)
        if args.action == "report" and args.output is not None and not args.output.exists():
            blocked = {"schema": 1, "state": "report_blocked", "error": str(error),
                       "limits": ["Publication/acceptance not established; inspect actual state before writes."]}
            try:
                save_result(blocked, args.output, args.summary)
            except (OSError, ValueError) as receipt_error:
                print(f"release: Cannot retain blocked report: {receipt_error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
