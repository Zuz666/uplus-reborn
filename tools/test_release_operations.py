"""Offline consumer-visible release status, uncertainty and advisory boundaries."""
from __future__ import annotations

import copy
import json
import unittest

from release import (
    advisory_delta,
    native_release_phases,
    release_outcome,
    validated_run_status,
)


class RunStatusTests(unittest.TestCase):
    def setUp(self):
        self.info = {"id": 1, "full_name": "owner/repo", "private": False, "default_branch": "master"}
        self.workflow = {"id": 2, "path": ".github/workflows/ci.yml"}
        self.sha = "a" * 40
        self.run = {"id": 3, "run_attempt": 2, "workflow_id": 2, "path": self.workflow["path"],
                    "repository": {"id": 1}, "head_repository": {"id": 1}, "head_sha": self.sha,
                    "head_branch": "master", "status": "completed", "conclusion": "success",
                    "html_url": "https://github.com/owner/repo/actions/runs/3"}
        self.jobs = [{"id": 4, "name": "CI", "run_id": 3, "run_attempt": 2,
                      "status": "completed", "conclusion": "success"}]

    def test_aggregate_green_with_pending_job_is_inconsistent_not_complete(self):
        self.jobs[0].update(status="in_progress", conclusion=None)
        result = validated_run_status(self.info, self.workflow, self.run, self.jobs, self.sha)
        self.assertEqual(result["state"], "inconsistent")
        self.assertEqual(result["publication_eligibility"], "not_evaluated")

    def test_other_sha_repository_workflow_branch_or_attempt_cannot_satisfy_status(self):
        for key, value in (("head_sha", "b" * 40), ("head_branch", "feature"),
                           ("head_repository", {"id": 9}), ("workflow_id", 99)):
            with self.subTest(key=key):
                run = {**self.run, key: value}
                with self.assertRaises(ValueError):
                    validated_run_status(self.info, self.workflow, run, self.jobs, self.sha)
        jobs = copy.deepcopy(self.jobs)
        jobs[0]["run_attempt"] = 1
        with self.assertRaises(ValueError):
            validated_run_status(self.info, self.workflow, self.run, jobs, self.sha)


class ReleaseOutcomeTests(unittest.TestCase):
    def test_audit_failure_is_not_success_or_publication(self):
        self.assertEqual(release_outcome(False, "failure", "skipped", "skipped"), "audit_blocked")

    def test_uncertain_write_never_becomes_safe_to_retry_or_accepted(self):
        self.assertEqual(release_outcome(True, "success", "failure", "skipped"),
                         "publication_unknown_inspect_before_writes")
        self.assertEqual(release_outcome(True, "failure", "skipped", "skipped"),
                         "publication_not_attempted")
        self.assertEqual(release_outcome(True, "success", "success", "failure"),
                         "published_acceptance_blocked")
        self.assertEqual(release_outcome(True, "success", "success", "success"), "published_and_accepted")

    def test_native_dependency_completion_survives_delayed_backend_status_without_publication(self):
        observed = {"repository": "owner/repo", "commit": "a" * 40, "run": {"id": 3, "run_attempt": 2}}
        environment = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "owner/repo",
                       "GITHUB_RUN_ID": "3", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
                       "GITHUB_WORKFLOW_REF": "owner/repo/.github/workflows/release.yml@refs/heads/master",
                       "UPLUS_RELEASE_NEEDS": json.dumps({name: {"result": result} for name, result in (
                           ("inspect", "success"), ("publish", "skipped"), ("hacs", "skipped"))})}
        phases = native_release_phases(observed, environment)
        self.assertEqual(release_outcome(False, *phases.values()), "audited_publication_not_requested")
        for field in ("GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_WORKFLOW_REF"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                native_release_phases(observed, {**environment, field: "foreign"})


class AdvisoryDeltaTests(unittest.TestCase):
    def test_known_findings_are_not_new_and_missing_open_alert_is_not_claimed_fixed(self):
        baseline = {"schema": 1, "status": "observed", "repository": "owner/repo", "repository_id": 1,
                    "alerts": [{"number": 1, "severity": "high"}, {"number": 2, "severity": "low"},
                               {"number": 3, "severity": "moderate"}]}
        current = {**baseline, "alerts": [{"number": 1, "severity": "high"},
                                         {"number": 2, "severity": "critical"}, {"number": 4, "severity": "low"}]}
        self.assertEqual(advisory_delta(current, baseline),
                         {"mode": "compared", "new": [4], "changed": [2], "no_longer_open": [3]})
        self.assertIsNone(advisory_delta(current, None)["new"])
        for invalid in ({**baseline, "status": "unavailable"}, {**baseline, "repository": "other/repo"}):
            with self.assertRaises(ValueError):
                advisory_delta(current, invalid)
