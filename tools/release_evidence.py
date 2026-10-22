"""Bind one committed candidate to a complete finite Core matrix.

These local producer/consumer operations never publish. GitHub run authorization
is separate from a self-reported receipt; a JSON file alone is not release authority.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import sys
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import github_api as github
from delivery import (
    _json,
    _no_links,
    append_github_output,
    docker_image_tag,
    load_artifact,
)
from release import git, release_metadata, verify_candidate

SCENARIOS = ("standby", "execution", "profiles", "discovery", "discovery-identification")
TARGETS = ("test", "core-current")
MAX_MATRIX_BYTES = 2 * 1024 * 1024
REQUIRED_JOBS = frozenset({
    "CI", "delivery / Committed release candidate", "delivery / Artifact handoff",
    "delivery / Isolated Core (test)", "delivery / Isolated Core (core-current)",
    "delivery / Public tooling (ubuntu-latest)", "delivery / Public tooling (windows-latest)",
    "documentation / Documentation (ubuntu-latest)", "documentation / Documentation (windows-latest)",
    "security / CodeQL (actions)", "security / CodeQL (javascript-typescript)", "security / CodeQL (python)",
    "validation / Python tests and lint", "validation / Workflow and secret checks",
    "validation / HACS validation", "validation / Hassfest",
    "validation / PR label rules (ubuntu-latest)", "validation / PR label rules (windows-latest)",
})
POLICY_FILES = (
    ".github/workflows/ci.yml", ".github/workflows/delivery.yml", ".github/workflows/validate.yml",
    ".github/workflows/release.yml", ".github/workflows/hacs-acceptance.yml", "tools/upgrade_smoke.py",
    ".github/workflows/docs.yml", ".github/workflows/codeql.yml", "tools/release_evidence.py",
    "tools/release.py", "tools/publish_release.py", "tools/delivery.py", "tools/ha_smoke.py", "tools/discovery_fixture.py",
    "tools/github_api.py",
    ".github/actions/build-image/action.yml",
    "tools/Dockerfile", "pyproject.toml", "uv.lock", "package.json", "package-lock.json",
)


def validate_protected_branch(metadata: dict, branch: dict, checks: list[dict]) -> None:
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


def choose_source_run(repository: dict, workflow: dict, metadata: dict, runs: list[dict]) -> dict:
    if (repository.get("private") is not False or repository.get("default_branch") != "master"
            or workflow.get("path") != ".github/workflows/ci.yml" or workflow.get("state") != "active"):
        raise ValueError("Expected public master and the active canonical CI workflow")
    candidates = [run for run in runs if (
        run.get("workflow_id") == workflow["id"] and run.get("path") == workflow["path"]
        and run.get("event") in {"push", "workflow_dispatch"} and run.get("head_branch") == "master"
        and run.get("head_sha") == metadata["commit"] and run.get("repository", {}).get("id") == repository["id"]
        and run.get("head_repository", {}).get("id") == repository["id"]
    )]
    if not candidates:
        exact = [run for run in runs if run.get("head_sha") == metadata["commit"]]
        raise ValueError(
            f"No trusted canonical master CI source for this exact SHA "
            f"(observed={len(runs)}, exact_sha={len(exact)}, "
            f"master={sum(run.get('head_branch') == 'master' for run in exact)}, "
            f"same_repository={sum(run.get('repository', {}).get('id') == repository['id'] for run in exact)}); "
            "inspect the exact run through release.py status; no write retry is authorized"
        )
    # Selection does not filter for green: a newer failed/pending run wins and blocks.
    latest = max(candidates, key=lambda run: (
        datetime.fromisoformat(run["created_at"]), run["id"], run["run_attempt"]))
    if (latest.get("status") != "completed" or latest.get("conclusion") != "success"
            or type(latest.get("run_attempt")) is not int or latest["run_attempt"] <= 0):
        raise ValueError(f"Latest trusted CI run {latest['id']} attempt {latest.get('run_attempt')} is not completed/successful")
    return latest


def validate_source_jobs(context: dict, jobs: list[dict]) -> dict:
    if len(jobs) != len(REQUIRED_JOBS) or {row.get("name") for row in jobs} != REQUIRED_JOBS:
        raise ValueError("Canonical CI is missing required jobs or has an undeclared job matrix")
    result = {}
    for row in jobs:
        prefix = f"https://api.github.com/repos/{context['repository']}/check-runs/"
        check_url = row.get("check_run_url", "")
        if (row.get("run_id") != context["run_id"] or row.get("run_attempt") != context["attempt"]
                or row.get("status") != "completed" or row.get("conclusion") != "success"
                or not check_url.startswith(prefix) or not re.fullmatch(r"[1-9][0-9]*", check_url.removeprefix(prefix))):
            raise ValueError(f"Unsuccessful or foreign source job: {row.get('name')}")
        result[row["name"]] = {key: row[key] for key in (
            "id", "run_id", "run_attempt", "status", "conclusion", "check_run_url", "started_at", "completed_at")}
    return result


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
    if (producer.get("schema") != 2 or producer.get("source") != context
            or producer.get("source_tree") != git(repo, "rev-parse", metadata["commit"] + "^{tree}").decode().strip()
            or producer.get("policy_hashes") != policy_hashes(repo, metadata["commit"])
            or producer.get("candidate") != provenance or producer.get("bundle") != load_artifact(candidate / "bundle")):
        raise ValueError("Candidate producer/source/policy identity differs")
    return producer


def produce(repo: Path, candidate: Path, metadata: dict, context: dict) -> dict:
    provenance = verify_candidate(repo, candidate, metadata)
    result = {
        "schema": 2, "source": context,
        "source_tree": git(repo, "rev-parse", metadata["commit"] + "^{tree}").decode().strip(),
        "policy_hashes": policy_hashes(repo, metadata["commit"]), "candidate": provenance,
        "bundle": load_artifact(candidate / "bundle"),
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
            "workflow_run": workflow, "size_in_bytes": row["size_in_bytes"],
            "created_at": row["created_at"], "expires_at": row["expires_at"]}


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
            "durations_seconds": {name: rows[name]["elapsed_seconds"] for name in SCENARIOS},
        }
    return {"schema": 2, "producer": producer,
            "candidate_artifact": uploaded,
            "targets": targets, "completed_at": datetime.now(UTC).isoformat()}


def select_source(repo: Path, repository: str, metadata: dict) -> dict:
    """Read authenticated backend state; never search for an arbitrary green CI."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected owner/repository")
    info = github.api(f"repos/{repository}")
    if info["full_name"] != repository:
        raise ValueError("Repository identity differs from the requested public source")
    workflow = github.api(f"repos/{repository}/actions/workflows/ci.yml")
    runs = github.pages(
        f"repos/{repository}/actions/workflows/{workflow['id']}/runs?per_page=100",
        key="workflow_runs")
    selected = choose_source_run(info, workflow, metadata, runs)
    current = github.api(f"repos/{repository}/actions/runs/{selected['id']}")
    if current["id"] != selected["id"]:
        raise ValueError("Source run changed during selection")
    run = choose_source_run(info, workflow, metadata, [current])
    context = {
        "repository": repository, "repository_id": info["id"], "head_repository_id": info["id"],
        "run_id": run["id"], "attempt": run["run_attempt"], "event": run["event"],
        "ref": "refs/heads/master", "head_branch": "master", "head_sha": metadata["commit"],
        "commit": metadata["commit"], "workflow_sha": metadata["commit"],
        "workflow_ref": f"{repository}/.github/workflows/ci.yml@refs/heads/master",
    }
    jobs = validate_source_jobs(context, github.pages(
        f"repos/{repository}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100", key="jobs"))
    checks = github.pages(
        f"repos/{repository}/commits/{metadata['commit']}/check-runs?filter=latest&per_page=100", key="check_runs")
    # Bind the protected CI context to this canonical run's actual job, not a
    # same-name green check from another workflow that uses the same GitHub App.
    check = github.api(jobs["CI"]["check_run_url"].removeprefix("https://api.github.com/"))
    checks = [row for row in checks if row.get("name") != "CI"] + [check]
    branch = github.api(f"repos/{repository}/branches/master")
    validate_protected_branch(metadata, branch, checks)
    required = branch["protection"]["required_status_checks"]
    required_names = set(required.get("contexts", [])) | {row["context"] for row in required.get("checks", [])}
    rows = github.pages(f"repos/{repository}/actions/runs/{run['id']}/artifacts?per_page=100", key="artifacts")
    suffix = f"{metadata['commit']}-{context['attempt']}"
    return {
        "schema": 1, "commit": metadata["commit"], "version": metadata["version"],
        "source_tree": git(repo, "rev-parse", metadata["commit"] + "^{tree}").decode().strip(),
        "repository": {"id": info["id"], "full_name": info["full_name"]},
        "workflow": {"id": workflow["id"], "path": workflow["path"]}, "source": context,
        "run": {key: run[key] for key in (
            "id", "run_attempt", "workflow_id", "path", "head_sha", "head_branch", "event",
            "status", "conclusion", "created_at", "updated_at", "html_url")},
        "jobs": jobs,
        "branch": {"name": branch["name"], "commit": {"sha": branch["commit"]["sha"]},
                   "protected": branch["protected"], "protection": {"required_status_checks": required}},
        "checks": [{**{key: row[key] for key in ("id", "name", "head_sha", "status", "conclusion")},
                    "app": {"id": row["app"]["id"], "slug": row["app"].get("slug", "")}}
                   for row in checks if row["name"] in required_names],
        "candidate_artifact": artifact_metadata(rows, f"candidate-{suffix}", context),
        "matrix_artifact": artifact_metadata(rows, f"matrix-evidence-{suffix}", context),
        "target_artifacts": {target: artifact_metadata(rows, f"core-{target}-{suffix}", context) for target in TARGETS},
        "selected_at": datetime.now(UTC).isoformat(),
    }


def validate_matrix_archive(raw: bytes, backend: dict, local_matrix: bytes) -> None:
    if (not 0 < backend["size_in_bytes"] <= MAX_MATRIX_BYTES or len(raw) != backend["size_in_bytes"]
            or "sha256:" + hashlib.sha256(raw).hexdigest() != backend["digest"]):
        raise ValueError("Matrix archive differs from authenticated backend size/digest")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if archive.namelist() != ["matrix-evidence.json"] or archive.infolist()[0].file_size > MAX_MATRIX_BYTES:
            raise ValueError("Unexpected matrix artifact members or size")
        if archive.read("matrix-evidence.json") != local_matrix:
            raise ValueError("Local matrix file differs from authenticated backend bytes")


def verify_source_evidence(repo: Path, repository: str, metadata: dict, candidate: Path,
                           matrix_path: Path, selection_path: Path) -> dict:
    """Recheck the selected attempt/artifacts and the committed-byte handoff."""
    selected = read_record(selection_path)
    fresh = select_source(repo, repository, metadata)
    for key in ("schema", "commit", "version", "source_tree", "repository", "workflow", "source",
                "jobs", "candidate_artifact", "matrix_artifact", "target_artifacts"):
        if selected.get(key) != fresh[key]:
            raise ValueError(f"Selected source/attempt/artifact changed: {key}; no publication is authorized")
    backend = fresh["matrix_artifact"]
    if not 0 < backend["size_in_bytes"] <= MAX_MATRIX_BYTES:
        raise ValueError("Matrix artifact exceeds its bounded evidence size")
    _no_links(matrix_path.absolute())
    raw = github.gh_bytes("api", f"repos/{repository}/actions/artifacts/{backend['id']}/zip")
    validate_matrix_archive(raw, backend, matrix_path.read_bytes())
    producer = verify_producer(repo, candidate, metadata, fresh["source"])
    matrix = read_record(matrix_path)
    if (matrix.get("schema") != 2 or matrix.get("producer") != producer
            or matrix.get("candidate_artifact") != fresh["candidate_artifact"]):
        raise ValueError("Matrix record does not bind the authenticated producer/candidate")
    targets = matrix.get("targets")
    if not isinstance(targets, dict) or set(targets) != set(TARGETS):
        raise ValueError("Matrix is missing required Core targets")
    versions = expected_core_versions(repo, metadata["commit"])
    for name, target in targets.items():
        if not isinstance(target, dict):
            raise TypeError("Expected a Core target evidence object")
        scenarios, environment = target.get("scenarios"), target.get("environment")
        if (not isinstance(scenarios, dict) or set(scenarios) != set(SCENARIOS)
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in scenarios.values())
                or target.get("artifact") != fresh["target_artifacts"][name]
                or target.get("core_version") != versions[name] or not isinstance(environment, dict)
                or environment.get("target") != name or environment.get("image") != docker_image_tag(repo, name)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", environment.get("image_id", ""))):
            raise ValueError(f"Missing or misbound Core matrix evidence: {name}")
    return {
        "schema": 1, "scenario": "release-evidence", "success": True, "status": "evidence_accepted",
        "publication_eligibility": "not_evaluated", "publication_requested": False,
        "selection": fresh, "candidate": producer["candidate"], "targets": targets,
        "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
        "verified_at": datetime.now(UTC).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("produce", "verify", "matrix", "select", "audit"):
        command = commands.add_parser(action)
        command.add_argument("--repo", type=Path, default=Path.cwd())
        command.add_argument("--commit", default=os.environ.get("GITHUB_SHA", "HEAD"))
        if action != "select":
            command.add_argument("--candidate", type=Path, required=True)
        if action in {"select", "audit"}:
            command.add_argument("--repository", required=True)
            command.add_argument("--version", required=True)
        if action in {"select", "audit", "matrix"}:
            command.add_argument("--output", type=Path, required=True)
        if action == "select":
            command.add_argument("--github-output", type=Path)
        if action == "audit":
            command.add_argument("--selection", type=Path, required=True)
            command.add_argument("--matrix", type=Path, required=True)
        if action == "matrix":
            command.add_argument("--receipts", type=Path, required=True)
            command.add_argument("--candidate-artifact-id", required=True)
            command.add_argument("--candidate-digest", required=True)
    args = parser.parse_args(argv)
    try:
        metadata = release_metadata(args.repo, args.commit, getattr(args, "version", None))
        if args.action == "select":
            result = select_source(args.repo, args.repository, metadata)
        elif args.action == "audit":
            result = verify_source_evidence(args.repo, args.repository, metadata, args.candidate, args.matrix, args.selection)
        else:
            context = context_from_environment(metadata["commit"])
            if args.action == "produce":
                result = produce(args.repo, args.candidate, metadata, context)
            elif args.action == "verify":
                result = verify_producer(args.repo, args.candidate, metadata, context)
            else:
                artifacts = github.pages(
                    f"repos/{context['repository']}/actions/runs/{context['run_id']}/artifacts?per_page=100", key="artifacts")
                result = collect_matrix(args.repo, args.candidate, metadata, context, args.receipts,
                                        args.candidate_artifact_id, args.candidate_digest, artifacts)
        if args.action in {"select", "audit", "matrix"}:
            _no_links(args.output.absolute())
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2, sort_keys=True)
                stream.write("\n")
        if args.action == "select" and args.github_output is not None:
            append_github_output(args.github_output, {
                "run_id": result["source"]["run_id"], "run_attempt": result["source"]["attempt"],
                "candidate_id": result["candidate_artifact"]["id"], "matrix_id": result["matrix_artifact"]["id"],
            })
        print(json.dumps({"verified": True, "action": args.action, "commit": metadata["commit"],
                          "source_run": result.get("source", result.get("selection", result.get("producer", {})).get("source", {})),
                          "publication_eligibility": "not_evaluated"}, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, github.GitHubOperationError) as error:
        print(f"release_evidence: {error}", file=sys.stderr)
        return 2 if isinstance(error, github.GitHubOperationError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
