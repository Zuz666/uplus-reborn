# Pull request

## Summary

Describe the user-visible problem and the single concern addressed. Link the issue.

## Scope and compatibility

- Integration version or base commit:
- Home Assistant versions:
- Affected behavior:
- Entity/device identity, history, units, options or saved-profile impact:
- Breaking change and migration, or none:

## Verification

List commands/scenarios actually run, their results and exact Core/container
versions. Explain why the selected checks observe the changed behavior. Mark
irrelevant layers N/A with a reason; do not claim a live appliance was tested
because a synthetic fixture passed.

- Behavior regression and executable smoke:
- Docker pytest/Ruff, if applicable:
- Public documentation/tooling, if applicable:
- Isolated Core or actual HACS installation, if applicable:
- Reused evidence and unchanged input binding, if any:

## Native UI proof

Attach sanitized screenshots/video for changed native UI behavior, or explain
N/A. Default desktop evidence uses 1920×1080 CSS px at DPR 1. No screenshots of
credentials, private identifiers or an unrelated live Home Assistant instance.

## Security and release impact

- Sensitive-data review:
- Control authorization, protocol or uncertain-outcome impact:
- Proposed user-facing release notes, or none:
- HACS/package/dependency impact:

## Checklist

- [ ] Commit/PR title follows Conventional Commits.
- [ ] Affected consumers, tests and public contracts agree.
- [ ] Evidence distinguishes source claims, software proof and physical observations.
- [ ] No secrets, private workspace, APKs or raw captures are included.
- [ ] Automated findings are addressed or explicitly justified.
- [ ] No ordinary-change version bump or unrelated release housekeeping is included.

## Review notes

Describe residual risks and any AI-assisted work relevant to review. Automated
review is advisory; the maintainer remains responsible for the accepted change.
