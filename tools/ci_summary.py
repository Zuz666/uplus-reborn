"""Append a secret-safe Job Summary from the five isolated Core smoke receipts.

Missing receipts are expected after an interrupted upstream step and do not fail
summary generation. Unreadable, malformed or invalid evidence returns exit 1,
while still appending the available evidence. A valid failed smoke receipt is
reported as failed, not reinterpreted as a summary-generation failure.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

SCENARIOS = (
    "standby", "execution", "profiles", "discovery", "discovery-identification",
)
TARGETS = ("test", "core-current")
STATUSES = frozenset({"passed", "failed", "not_run", "not_applicable"})
# Selection reasons are fixed public categories, never free-form event data.
SELECTION_REASONS = frozenset({
    "local_default", "local_explicit", "forced_full",
    "non_pull_request", "unrelated_changes", "relevant_changes", "unknown_changes",
    "incomplete_comparison", "unavailable_comparison", "stale_comparison",
})


def _seconds(value: Any) -> str | None:
    if type(value) not in (int, float):
        return None
    try:
        if not math.isfinite(value) or value < 0:
            return None
    except OverflowError:
        return None
    return f"{value:.3f} s"


def _status(value: Any) -> str | None:
    return value if isinstance(value, str) and value in STATUSES else None


def render_summary(receipts: Path, target: str) -> tuple[str, list[str], bool]:
    """Return Markdown, safe diagnostics and whether any present evidence is invalid."""
    rows = []
    diagnostics: list[str] = []
    invalid = False
    discovery: dict[str, Any] | None = None
    for scenario in SCENARIOS:
        receipt = None
        try:
            raw = (receipts / f"{scenario}.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            diagnostics.append(f"{scenario}: receipt missing")
        except (OSError, UnicodeError):
            diagnostics.append(f"{scenario}: receipt unreadable")
            invalid = True
        else:
            try:
                candidate = json.loads(raw)
            except (ValueError, RecursionError):
                diagnostics.append(f"{scenario}: receipt malformed")
                invalid = True
            else:
                if (
                    not isinstance(candidate, dict)
                    or type(candidate.get("schema")) is not int
                    or candidate["schema"] != 1
                    or candidate.get("scenario") != scenario
                ):
                    diagnostics.append(f"{scenario}: receipt identity invalid")
                    invalid = True
                else:
                    receipt = candidate
        status = "unavailable"
        elapsed = "unavailable"
        if receipt is not None:
            success = receipt.get("success")
            if type(success) is bool:
                status = "passed" if success else "failed"
            else:
                diagnostics.append(f"{scenario}: success unavailable or invalid")
                invalid = True
            measured = _seconds(receipt.get("elapsed_seconds"))
            if measured is None:
                diagnostics.append(f"{scenario}: elapsed_seconds unavailable or invalid")
                invalid = True
            else:
                elapsed = measured
            if scenario == "discovery":
                discovery = receipt
        rows.append(f"| {scenario} | {status} | {elapsed} |")

    lines = [
        f"## Isolated Core acceptance ({target})", "",
        "| Scenario | Status | Measured elapsed |", "| --- | --- | --- |", *rows,
        "", "### Discovery timing", "",
    ]
    timing_value = discovery.get("discovery_timing") if discovery is not None else None
    timing = timing_value if isinstance(timing_value, dict) else {}
    selection = timing_value if isinstance(timing_value, dict) else (
        discovery.get("discovery_selection") if discovery is not None else None
    )
    if discovery is not None and not isinstance(timing_value, dict) and (
        "discovery_timing" in discovery
        or discovery.get("success") is not False
        or not isinstance(selection, dict)
    ):
        diagnostics.append("discovery: discovery_timing unavailable or invalid")
        invalid = True
    if not isinstance(selection, dict):
        lines.extend(["Mode: unavailable. Selection reason: unavailable.", ""])
    else:
        mode = selection.get("mode")
        if mode not in ("full", "fast"):
            mode = "unavailable"
            diagnostics.append("discovery: timing mode unavailable or invalid")
            invalid = True
        reason = selection.get("reason")
        if not isinstance(reason, str) or reason not in SELECTION_REASONS:
            # Even a syntactically safe unknown token could be a credential.
            reason = "unavailable (unrecognized or missing category; redacted)"
            diagnostics.append("discovery: selection reason unavailable or unrecognized")
            invalid = True
        lines.extend([f"Mode: **{mode}**. Selection reason: {reason}.", ""])
        if mode == "fast":
            lines.extend([
                ("Fast mode uses controlled discovery scheduling; it does not prove actual "
                 "20-second packet spacing or 122 seconds of continuous real-time search."), "",
            ])

    lines.extend(["| Real-time proof | Status |", "| --- | --- |"])
    real_time = timing.get("real_time")
    for check in ("cadence", "continuous"):
        observed = real_time.get(check) if isinstance(real_time, dict) else None
        status = _status(observed)
        if timing.get("mode") == "fast" and status in ("passed", "failed"):
            status = None
        if status is None and discovery is not None and timing:
            diagnostics.append(f"discovery: real_time.{check} unavailable or invalid")
            invalid = True
        lines.append(f"| {check} | {status or 'unavailable'} |")

    lines.extend([
        "", "| Discovery phase | Status | Measured elapsed |", "| --- | --- | --- |",
    ])
    phases = timing.get("phases")
    for name in ("baseline", "lifecycle", "shared_sender"):
        phase = phases.get(name) if isinstance(phases, dict) else None
        status = _status(phase.get("status")) if isinstance(phase, dict) else None
        elapsed = _seconds(phase.get("elapsed_seconds")) if isinstance(phase, dict) else None
        if status is None and discovery is not None and timing:
            diagnostics.append(f"discovery: phase {name} status unavailable or invalid")
            invalid = True
        if elapsed is None and status not in (None, "not_run", "not_applicable"):
            diagnostics.append(f"discovery: phase {name} elapsed_seconds unavailable or invalid")
            invalid = True
        lines.append(f"| {name} | {status or 'unavailable'} | {elapsed or 'unavailable'} |")
    lines.extend([
        "", ("Phase durations are actual elapsed measurements, may overlap, and are not additive. "
             "They are not a replacement for the discovery scenario's total elapsed time."),
        "Raw receipts remain authoritative; unavailable evidence is neither zero nor success.", "",
    ])
    if diagnostics:
        lines.extend(["### Evidence diagnostics", "", *(f"- {item}" for item in diagnostics), ""])
    return "\n".join(lines), diagnostics, invalid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary, diagnostics, invalid = render_summary(args.receipts, args.target)
    for diagnostic in diagnostics:
        print(f"ci_summary: {diagnostic}", file=sys.stderr)
    try:
        with args.output.open("a", encoding="utf-8", newline="\n") as output:
            output.write("\n" + summary)
    except (OSError, UnicodeError):
        print("ci_summary: summary output unavailable", file=sys.stderr)
        return 1
    return int(invalid)


if __name__ == "__main__":
    raise SystemExit(main())
