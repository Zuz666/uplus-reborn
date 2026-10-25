"""Standalone receipt summary contracts; run with unittest discovery in tools."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).with_name("ci_summary.py").resolve()
SCENARIOS = ("standby", "execution", "profiles", "discovery", "discovery-identification")


class SummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.receipts = self.root / "receipts"
        self.receipts.mkdir()
        self.output = self.root / "summary.md"

    def write_receipt(self, name, **changes):
        receipt = {"schema": 1, "scenario": name, "success": True, "elapsed_seconds": 1.25}
        if name == "discovery":
            receipt["discovery_timing"] = {
                "mode": "full", "reason": "relevant_changes",
                "real_time": {"cadence": "passed", "continuous": "passed"},
                "phases": {
                    "baseline": {"status": "passed", "elapsed_seconds": 2.5},
                    "lifecycle": {"status": "passed", "elapsed_seconds": 7.25},
                    "shared_sender": {"status": "passed", "elapsed_seconds": 5.125},
                },
            }
        receipt.update(changes)
        (self.receipts / f"{name}.json").write_text(json.dumps(receipt), encoding="utf-8")
        return receipt

    def write_all(self):
        for scenario in SCENARIOS:
            self.write_receipt(scenario)

    def cli(self, target="test", expected=0, output=None):
        result = subprocess.run(
            [sys.executable, str(TOOL), "--receipts", str(self.receipts),
             "--target", target, "--output", str(output or self.output)],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def text(self):
        return self.output.read_text(encoding="utf-8")

    def test_all_five_scenarios_and_both_targets_append_measured_evidence(self):
        self.write_all()
        previous = "Existing summary with unrelated content.\n"
        self.output.write_text(previous, encoding="utf-8")
        for target in ("test", "core-current"):
            with self.subTest(target=target):
                result = self.cli(target)
                self.assertEqual(result.stderr, "")
        summary = self.text()
        self.assertTrue(summary.startswith(previous))
        self.assertIn("Isolated Core acceptance (test)", summary)
        self.assertIn("Isolated Core acceptance (core-current)", summary)
        for scenario in SCENARIOS:
            self.assertEqual(summary.count(f"| {scenario} | passed | 1.250 s |"), 2)
        self.assertIn("Mode: **full**", summary)
        self.assertIn("relevant_changes", summary)
        for phase, measured in (("baseline", "2.500"), ("lifecycle", "7.250"), ("shared_sender", "5.125")):
            self.assertIn(f"| {phase} | passed | {measured} s |", summary)

    def test_failed_receipt_is_visible_without_replaying_error_or_changing_exit(self):
        self.write_all()
        self.write_receipt(
            "execution", success=False, elapsed_seconds=3.875,
            error="SECRET_TOKEN=hidden\n<script>leak()</script>",
            observations=[{"password": "do-not-print"}],
        )
        result = self.cli()
        self.assertEqual(result.stderr, "")
        self.assertIn("| execution | failed | 3.875 s |", self.text())
        for value in ("SECRET_TOKEN", "<script>", "do-not-print"):
            self.assertNotIn(value, self.text() + result.stdout + result.stderr)

    def test_missing_receipts_are_unavailable_not_zero_or_success(self):
        result = self.cli()
        summary = self.text()
        for scenario in SCENARIOS:
            self.assertIn(f"| {scenario} | unavailable | unavailable |", summary)
            self.assertIn(f"{scenario}: receipt missing", result.stderr)
        self.assertIn("Mode: unavailable", summary)
        self.assertIn("| baseline | unavailable | unavailable |", summary)
        self.assertNotIn("0.000 s", summary)
        self.assertNotIn("| passed |", summary)

    def test_malformed_unreadable_and_invalid_unicode_surface_safe_diagnostics(self):
        self.write_all()
        (self.receipts / "standby.json").write_text('{"SECRET_TOKEN": invalid}', encoding="utf-8")
        (self.receipts / "profiles.json").unlink()
        (self.receipts / "profiles.json").mkdir()
        (self.receipts / "execution.json").write_bytes(b"\xffSECRET_TOKEN")
        result = self.cli(expected=1)
        summary = self.text()
        self.assertIn("standby: receipt malformed", result.stderr)
        self.assertIn("profiles: receipt unreadable", result.stderr)
        self.assertIn("execution: receipt unreadable", result.stderr)
        self.assertIn("| discovery | passed | 1.250 s |", summary)
        self.assertNotIn("SECRET_TOKEN", summary + result.stdout + result.stderr)

    def test_receipt_identity_is_required_before_success_is_trusted(self):
        for value in (
            [], None, {"schema": True},
            {"schema": 2, "scenario": "standby", "success": True},
            {"schema": 1, "scenario": "execution", "success": True},
        ):
            with self.subTest(value=value):
                (self.receipts / "standby.json").write_text(json.dumps(value), encoding="utf-8")
                self.cli(expected=1)
        summary = self.text()
        self.assertIn("receipt identity invalid", summary)
        self.assertNotIn("| standby | passed |", summary)

    def test_invalid_or_missing_fields_are_unavailable_not_coerced(self):
        self.write_all()
        for success, elapsed in (
            ("true", "1.25"), (1, True), (None, None),
            ([], -1), ({}, float("nan")), (False, float("inf")),
            (True, 10 ** 400),
        ):
            with self.subTest(success=success, elapsed=elapsed):
                self.output.unlink(missing_ok=True)
                self.write_receipt("standby", success=success, elapsed_seconds=elapsed)
                self.cli(expected=1)
                summary = self.text()
                status = "passed" if success is True else "failed" if success is False else "unavailable"
                self.assertIn(f"| standby | {status} | unavailable |", summary)
        (self.receipts / "standby.json").write_text(
            '{"schema": 1, "scenario": "standby"}', encoding="utf-8",
        )
        self.output.unlink()
        self.cli(expected=1)
        self.assertIn("| standby | unavailable | unavailable |", self.text())

    def test_observed_zero_is_distinct_from_unavailable(self):
        self.write_all()
        self.write_receipt("standby", elapsed_seconds=0)
        self.cli()
        self.assertIn("| standby | passed | 0.000 s |", self.text())

    def test_fast_reports_controlled_time_and_omitted_real_time_proof(self):
        self.write_all()
        receipt = self.write_receipt("discovery", elapsed_seconds=8.75)
        timing = receipt["discovery_timing"]
        timing.update(
            mode="fast", reason="unrelated_changes",
            real_time={"cadence": "not_applicable", "continuous": "not_applicable"},
        )
        self.write_receipt("discovery", **receipt)
        self.cli()
        summary = self.text()
        self.assertIn("Mode: **fast**", summary)
        self.assertIn("unrelated_changes", summary)
        self.assertIn("| discovery | passed | 8.750 s |", summary)
        self.assertIn("| cadence | not_applicable |", summary)
        self.assertIn("| continuous | not_applicable |", summary)

    def test_fast_receipt_cannot_claim_passed_real_time_proof(self):
        self.write_all()
        receipt = self.write_receipt("discovery")
        receipt["discovery_timing"]["mode"] = "fast"
        self.write_receipt("discovery", **receipt)
        self.cli(expected=1)
        self.assertIn("| cadence | unavailable |", self.text())
        self.assertIn("| continuous | unavailable |", self.text())

    def test_failed_partial_discovery_preserves_unexecuted_phases(self):
        self.write_all()
        receipt = self.write_receipt("discovery", success=False, elapsed_seconds=6.5)
        receipt["discovery_timing"].update(
            real_time={"cadence": "not_run", "continuous": "not_run"},
            phases={
                "baseline": {"status": "failed", "elapsed_seconds": 2.125},
                "lifecycle": {"status": "not_run", "elapsed_seconds": None},
                "shared_sender": {"status": "not_run"},
            },
        )
        self.write_receipt("discovery", **receipt)
        self.cli()
        summary = self.text()
        self.assertIn("| discovery | failed | 6.500 s |", summary)
        self.assertIn("| baseline | failed | 2.125 s |", summary)
        self.assertIn("| lifecycle | not_run | unavailable |", summary)
        self.assertIn("| shared_sender | not_run | unavailable |", summary)
        self.assertIn("| cadence | not_run |", summary)

    def test_early_failure_uses_selection_without_claiming_campaign_execution(self):
        self.write_all()
        receipt = self.write_receipt("discovery", success=False, elapsed_seconds=0.125)
        del receipt["discovery_timing"]
        receipt["discovery_selection"] = {
            "mode": "fast", "reason": "unrelated_changes",
            "tested_commit": "SECRET_TOKEN_DO_NOT_PRINT",
        }
        (self.receipts / "discovery.json").write_text(json.dumps(receipt), encoding="utf-8")
        result = self.cli()
        summary = self.text()
        self.assertIn("Mode: **fast**", summary)
        self.assertIn("Selection reason: unrelated_changes.", summary)
        self.assertIn("| discovery | failed | 0.125 s |", summary)
        self.assertIn("| baseline | unavailable | unavailable |", summary)
        self.assertIn("| cadence | unavailable |", summary)
        self.assertNotIn("SECRET_TOKEN", summary + result.stderr)

    def test_unknown_reason_and_nested_values_are_redacted_not_echoed(self):
        self.write_all()
        secret = "<script>SECRET_TOKEN</script>|\n# forged-heading"
        receipt = self.write_receipt("discovery")
        receipt["discovery_timing"].update(
            reason=secret, real_time={"cadence": secret, "continuous": {"secret": secret}},
            phases={"baseline": {"status": secret, "elapsed_seconds": secret}},
        )
        self.write_receipt("discovery", **receipt)
        result = self.cli(expected=1)
        evidence = self.text() + result.stdout + result.stderr
        self.assertNotIn("SECRET_TOKEN", evidence)
        self.assertNotIn("forged-heading", evidence)
        self.assertNotIn("<script>", evidence)
        self.assertIn("redacted", evidence)
        self.assertIn("| baseline | unavailable | unavailable |", evidence)

    def test_missing_timing_and_invalid_phase_measurement_are_diagnostic(self):
        self.write_all()
        self.write_receipt("discovery", discovery_timing=None)
        self.cli(expected=1)
        self.assertIn("discovery_timing unavailable or invalid", self.text())
        self.output.unlink()
        receipt = self.write_receipt("discovery")
        receipt["discovery_timing"]["phases"]["baseline"]["elapsed_seconds"] = -1
        self.write_receipt("discovery", **receipt)
        self.cli(expected=1)
        self.assertIn("| baseline | passed | unavailable |", self.text())

    def test_selector_and_local_fixed_reason_categories_are_supported(self):
        self.write_all()
        for reason in (
            "forced_full", "non_pull_request", "unavailable_comparison", "incomplete_comparison",
            "stale_comparison", "relevant_changes", "unknown_changes", "unrelated_changes",
            "local_default", "local_explicit",
        ):
            with self.subTest(reason=reason):
                receipt = self.write_receipt("discovery")
                receipt["discovery_timing"]["reason"] = reason
                self.write_receipt("discovery", **receipt)
                self.cli()
                self.assertIn(f"Selection reason: {reason}.", self.text())

    def test_output_failure_is_reported_without_echoing_exception_details(self):
        self.write_all()
        result = self.cli(output=self.root / "SECRET_PATH" / "summary.md", expected=1)
        self.assertIn("summary output unavailable", result.stderr)
        self.assertNotIn("SECRET_PATH", result.stderr)

    def test_target_argument_accepts_only_supported_core_targets(self):
        result = self.cli(target="unknown", expected=2)
        self.assertIn("invalid choice", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
