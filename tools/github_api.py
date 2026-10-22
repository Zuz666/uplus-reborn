"""Small read/write GitHub CLI transport for public release tools.

No retries: callers must inspect actual state after an uncertain write. Errors
retain only operation/status, never credentials, payloads or signed URLs.
"""
from __future__ import annotations

import json
import os
import re
import subprocess


class GitHubOperationError(RuntimeError):
    """An unavailable/failed transport is not verified release acceptance."""


def _invoke(args: tuple[str, ...], payload: dict | None) -> bytes:
    command = ["gh", *args]
    if payload is not None:
        command.extend(["--method", "POST", "--input", "-"])
    try:
        result = subprocess.run(
            command, input=json.dumps(payload).encode("utf-8") if payload is not None else None,
            capture_output=True, check=False, timeout=120,
            env=dict(os.environ, GH_HOST="github.com"),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitHubOperationError(
            "GitHub client unavailable or timed out; inspect actual state before retrying any write"
        ) from error
    if result.returncode:
        status = re.search(r"\bHTTP ([1-5][0-9]{2})\b", result.stderr.decode("utf-8", "replace"))
        detail = f"HTTP {status.group(1)}" if status else f"exit {result.returncode}"
        raise GitHubOperationError(
            f"GitHub {args[0]} operation failed ({detail}); inspect actual state before retrying any write"
        )
    return result.stdout


def gh(*args: str, payload: dict | None = None) -> str:
    return _invoke(args, payload).decode("utf-8")


def gh_bytes(*args: str) -> bytes:
    return _invoke(args, None)


def api(path: str, *, payload: dict | None = None) -> dict | list:
    return json.loads(gh("api", path, payload=payload))


def pages(path: str, *, key: str | None = None) -> list:
    result = []
    for page in json.loads(gh("api", "--paginate", "--slurp", path)):
        rows = page[key] if key is not None else page
        if not isinstance(rows, list):
            raise TypeError("Expected a paginated GitHub list")
        result.extend(rows)
    return result


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
