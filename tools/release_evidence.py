"""Bind one committed candidate to a complete finite Core matrix.

These local producer/consumer operations never publish. GitHub run authorization
is separate from a self-reported receipt; a JSON file alone is not release authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from delivery import _json, _no_links, docker_image_tag, load_artifact
from publish_release import gh
from release import git, release_metadata, verify_candidate

SCENARIOS = ("standby", "execution", "profiles", "discovery", "discovery-identification")
TARGETS = ("test", "core-current")
POLICY_FILES = (
    ".github/workflows/ci.yml", ".github/workflows/delivery.yml", ".github/workflows/validate.yml",
    ".github/workflows/docs.yml", ".github/workflows/codeql.yml", "tools/release_evidence.py",
    "tools/release.py", "tools/publish_release.py", "tools/delivery.py", "tools/ha_smoke.py", "tools/discovery_fixture.py",
    "tools/Dockerfile", "pyproject.toml", "uv.lock", "package.json", "package-lock.json",
)


def read_record(path: Path) -> dict:
    _no_links(path.absolute())
    return _json(path)


def digest(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Missing artifact digest")
    value = value.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Missing valid artifact digest")
    return "sha256:" + value


def context_from_environment(commit: str) -> dict:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    head = (event["pull_request"]["head"] if event.get("pull_request") is not None else {
        "sha": commit, "repo": {"id": int(os.environ["GITHUB_REPOSITORY_ID"])}, "ref": os.environ["GITHUB_REF_NAME"],
    })
    if os.environ["GITHUB_SHA"] != commit:
        raise ValueError("Checkout differs from the selected workflow commit")
    context = {
        "repository": os.environ["GITHUB_REPOSITORY"],
        "repository_id": int(os.environ["GITHUB_REPOSITORY_ID"]),
        "run_id": int(os.environ["GITHUB_RUN_ID"]), "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "event": os.environ["GITHUB_EVENT_NAME"], "ref": os.environ["GITHUB_REF"],
        "workflow_ref": os.environ["GITHUB_WORKFLOW_REF"], "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"],
        "head_sha": head["sha"], "head_repository_id": int(head["repo"]["id"]),
        "head_branch": head["ref"], "commit": commit,
    }
    if any(context[key] <= 0 for key in ("repository_id", "head_repository_id", "run_id", "attempt")):
        raise ValueError("Missing positive workflow identity")
    for key in ("head_sha", "commit", "workflow_sha"):
        if not re.fullmatch(r"[0-9a-f]{40}", context[key]):
            raise ValueError("Missing workflow source identity")
    return context


def policy_hashes(repo: Path, commit: str) -> dict:
    return {path: hashlib.sha256(git(repo, "show", f"{commit}:{path}")).hexdigest() for path in POLICY_FILES}


def verify_producer(repo: Path, candidate: Path, metadata: dict, context: dict) -> dict:
    provenance = verify_candidate(repo, candidate, metadata)
    producer = read_record(candidate / "producer.json")
    if (producer.get("schema") != 1 or producer.get("source") != context
            or producer.get("source_tree") != git(repo, "rev-parse", metadata["commit"] + "^{tree}").decode().strip()
            or producer.get("policy_hashes") != policy_hashes(repo, metadata["commit"])
            or producer.get("candidate") != provenance):
        raise ValueError("Candidate producer/source/policy identity differs")
    return producer


def produce(repo: Path, candidate: Path, metadata: dict, context: dict) -> dict:
    provenance = verify_candidate(repo, candidate, metadata)
    result = {
        "schema": 1, "source": context,
        "source_tree": git(repo, "rev-parse", metadata["commit"] + "^{tree}").decode().strip(),
        "policy_hashes": policy_hashes(repo, metadata["commit"]), "candidate": provenance,
        "host_python": platform.python_version(), "created_at": datetime.now(UTC).isoformat(),
    }
    with (candidate / "producer.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return result


def expected_core_versions(repo: Path, commit: str) -> dict[str, str]:
    locked = tomllib.loads(git(repo, "show", f"{commit}:uv.lock").decode())
    minimum = [p["version"] for p in locked["package"] if p["name"] == "homeassistant"]
    recipe = git(repo, "show", f"{commit}:tools/Dockerfile").decode()
    current = re.search(r"(?m)^FROM ghcr\.io/home-assistant/home-assistant:([^@ ]+)@sha256:[0-9a-f]{64} AS core-current$", recipe)
    if len(minimum) != 1 or current is None:
        raise ValueError("Missing pinned minimum/current Core version")
    return {"test": minimum[0], "core-current": current.group(1)}


def validate_core_receipts(rows: dict[str, dict], artifact: dict, expected_version: str) -> None:
    if set(rows) != set(SCENARIOS):
        raise ValueError("Required scenario coverage is incomplete or unexpected")
    for scenario, row in rows.items():
        if (row.get("schema") != 1 or row.get("scenario") != scenario or row.get("success") is not True
                or row.get("temporary_config_removed") is not True or row.get("artifact") != artifact
                or row.get("core_version") != expected_version
                or row.get("installed_hashes_before") != artifact["files"]
                or row.get("installed_hashes_after") != artifact["files"]
                or row.get("shutdown", {}).get("core_stopped") is not True):
            raise ValueError(f"Incomplete or misbound {scenario} Core evidence")
        if scenario == "profiles":
            restart = row.get("restart", {})
            if restart.get("core_version") != expected_version or restart.get("shutdown", {}).get("core_stopped") is not True:
                raise ValueError("Missing profile persistence restart evidence")


def artifact_metadata(rows: list[dict], name: str, context: dict) -> dict:
    matches = [row for row in rows if row.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"Missing or ambiguous artifact: {name}")
    row = matches[0]
    workflow = row.get("workflow_run")
    expected = {"id": context["run_id"], "repository_id": context["repository_id"],
                "head_repository_id": context["head_repository_id"],
                "head_sha": context["head_sha"], "head_branch": context["head_branch"]}
    if (row.get("expired") is not False or type(row.get("id")) is not int or row["id"] <= 0
            or type(row.get("size_in_bytes")) is not int or row["size_in_bytes"] <= 0
            or not isinstance(workflow, dict) or any(workflow.get(key) != value for key, value in expected.items())):
        raise ValueError(f"Expired or foreign artifact: {name}")
    return {"id": row["id"], "name": name, "digest": digest(row.get("digest")),
            "workflow_run": workflow, "created_at": row["created_at"], "expires_at": row["expires_at"]}


def collect_matrix(repo: Path, candidate: Path, metadata: dict, context: dict,
                   receipts: Path, artifact_id: str, artifact_digest: str, artifacts: list[dict]) -> dict:
    producer = verify_producer(repo, candidate, metadata, context)
    if not re.fullmatch(r"[1-9][0-9]*", artifact_id):
        raise ValueError("Missing immutable candidate artifact ID")
    uploaded = artifact_metadata(artifacts, f"candidate-{metadata['commit']}-{context['attempt']}", context)
    if uploaded["id"] != int(artifact_id) or uploaded["digest"] != digest(artifact_digest):
        raise ValueError("Transferred candidate differs from backend ID/digest")
    artifact = load_artifact(candidate / "bundle")
    versions = expected_core_versions(repo, metadata["commit"])
    targets = {}
    for target in TARGETS:
        directory = receipts / target
        rows = {name: read_record(directory / f"{name}.json") for name in SCENARIOS}
        validate_core_receipts(rows, artifact, versions[target])
        environment = read_record(directory / "environment.json")
        if (environment.get("target") != target or environment.get("image") != docker_image_tag(repo, target)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", environment.get("image_id", ""))):
            raise ValueError("Core image/target identity differs")
        targets[target] = {
            "core_version": versions[target], "environment": environment,
            "artifact": artifact_metadata(artifacts, f"core-{target}-{metadata['commit']}-{context['attempt']}", context),
            "scenarios": {name: hashlib.sha256((directory / f"{name}.json").read_bytes()).hexdigest() for name in SCENARIOS},
        }
    return {"schema": 1, "producer": producer,
            "candidate_artifact": uploaded,
            "targets": targets, "completed_at": datetime.now(UTC).isoformat()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("produce", "verify", "matrix"):
        command = commands.add_parser(action)
        command.add_argument("--repo", type=Path, default=Path.cwd())
        command.add_argument("--commit", default=os.environ.get("GITHUB_SHA", "HEAD"))
        command.add_argument("--candidate", type=Path, required=True)
        if action == "matrix":
            command.add_argument("--receipts", type=Path, required=True)
            command.add_argument("--candidate-artifact-id", required=True)
            command.add_argument("--candidate-digest", required=True)
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        metadata = release_metadata(args.repo, args.commit)
        context = context_from_environment(metadata["commit"])
        if args.action == "produce":
            result = produce(args.repo, args.candidate, metadata, context)
        elif args.action == "verify":
            result = verify_producer(args.repo, args.candidate, metadata, context)
        else:
            pages = json.loads(gh("api", "--paginate", "--slurp",
                                 f"repos/{context['repository']}/actions/runs/{context['run_id']}/artifacts?per_page=100"))
            artifacts = [artifact for page in pages for artifact in page["artifacts"]]
            result = collect_matrix(args.repo, args.candidate, metadata, context, args.receipts,
                                    args.candidate_artifact_id, args.candidate_digest, artifacts)
            _no_links(args.output.absolute())
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2, sort_keys=True)
                stream.write("\n")
        print(json.dumps({"verified": True, "action": args.action, "commit": metadata["commit"],
                          "source_run": result.get("source", result.get("producer", {}).get("source", {}))}, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"release_evidence: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
