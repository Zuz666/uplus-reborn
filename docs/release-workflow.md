# Release workflow

U+ Reborn is distributed as a **HACS Custom repository**, category Integration.
This is not acceptance into the default HACS catalogue. The only supported
appliance is Haier HWD120-B1558U; software tests do not imply additional hardware
coverage. See [CONTRIBUTING](../CONTRIBUTING.md) and
[the verification contract](development-verification.md).

## Prepare a candidate

1. Use a release-preparation PR against `master`. Select a stable SemVer version,
   without a `v` prefix in metadata. The first public release is **1.0.0**;
   unpublished development versions are not earlier public releases.
2. Set the same version in `custom_components/uplus_reborn/manifest.json` and
   `pyproject.toml`; run `uv lock` and commit the matching root package in
   `uv.lock`. Ordinary feature/fix/dependency PRs do not bump these versions.
3. Add a dated `## [VERSION] - YYYY-MM-DD` section at the top of `CHANGELOG.md`.
   Curate user-visible changes, migrations/breaking changes and known limits.
   Do not fabricate historical releases or substitute generated commit titles.
4. Keep `hacs.json` in source-tree mode during first-repository bootstrap. After
   an actual bootstrap installation succeeds, set `zip_release: true`,
   `filename: uplus_reborn.zip` and `hide_default_branch: true` in the release PR.
   ZIP mode has no guaranteed source-branch fallback before a release exists.
5. Review the exact diff and sensitive material. Include the MIT license in the
   integration package; documentation remains separately CC BY 4.0. Keep private
   instructions, plans, captures, APKs, credentials and generated files out.
6. Pass protected PR CI and merge the accepted candidate. Wait for the complete
   CI matrix on that exact public `master` SHA; record its run and attempt.
   PR synthetic-merge evidence cannot authorize the later squash commit.

Initial publication starts from one independent, reviewed root snapshot. Never
mirror an unrelated development history, publish private provenance, or replace
the local development repository to achieve that boundary. Subsequent public
changes use ordinary PR history.

## Build committed bytes

Use Python 3.13+ and Git from a clean public checkout; no private workspace is
required. Output directories must not already exist.

```sh
uv run --no-project --python 3.13 python tools/release.py check --commit HEAD
uv run --no-project --python 3.13 python tools/release.py build --commit HEAD --output build/release-candidate
```

The CLI reads metadata, notes and integration files from the selected commit,
not the working tree. It rejects version/lock mismatches, missing release notes,
missing license/runtime files, unsafe archive members and occupied output paths.
It reuses the existing committed-byte artifact validator rather than defining
another extraction policy.

The output contains:

- `uplus_reborn.zip`: integration contents at archive root, with no extra
  `custom_components/uplus_reborn/` directory;
- `SHA256SUMS`: the ZIP's SHA-256;
- `release.json`: public source commit, version, ZIP digest and member hashes;
- `release-notes.md`: only the selected changelog section;
- `bundle/`: the validated tar/manifest input for isolated Core acceptance, not
  an additional HACS download asset.

Only the ZIP, checksum and sanitized provenance are release assets. Hashes prove
byte integrity, not a signature, security audit or hardware compatibility. Never
package a dirty checkout or use an unavailable private commit in public provenance.

## CI candidate and matrix handoff

Each CI run produces one committed candidate, then both isolated Core targets
download that same immutable Actions artifact ID. The candidate is not rebuilt
in each matrix job. Source verification reads actual committed blob hashes
without generating another package. Transfer digest validation remains enabled.

Schema-2 `producer.json` binds the ZIP and complete bundle archive/member map
to repository identity, tested commit, source tree, workflow source,
run/attempt and explicit policy/input hashes.
For pull requests the API head and tested synthetic merge SHA are distinct:
neither is silently substituted for the other.

Each Core target retains only its environment and five scenario receipts.
The handoff collector requires standby, execution, profiles with persistence
restart, discovery and discovery-identification on both pinned Core versions.
It rejects missing/failed scenarios, wrong source or Core versions, changed
installed members, incomplete shutdown and missing temporary-config cleanup.

The `matrix-evidence-SHA-ATTEMPT` artifact records that candidate's backend
ID/digest, per-target receipt hashes, image identities and backend receipt
artifact metadata. Names include the attempt to avoid mixing reruns. These
records describe runtime evidence, not a self-issued publication authorization:
the aggregate CI and deliberate publication gates still apply.

### Trusted master consumption

Release consumes the canonical `.github/workflows/ci.yml` run for the identical
accepted master SHA. It does not call CI again or rebuild that candidate.
Authenticated backend records must identify this repository, a master push or
explicit master CI dispatch, the current successful attempt and all 18 declared
successful jobs. The required protected `CI` check is bound to that run's actual
job, not an arbitrary same-name check from the same GitHub App.

Selection never searches backward for green: a newer failed, cancelled or
incomplete trusted run blocks release. PR/fork/other-workflow evidence is not
a release source. Recheck current master/protection, source attempt and immutable
artifact identities before the first write and after draft upload.

Candidate and schema-2 matrix artifacts are downloaded by backend ID/run with
digest checks enabled. The verifier also retrieves the matrix archive directly
by ID, validates its server digest and bounded single-member layout, and compares
the actual JSON bytes. A locally invented success record cannot authorize release.

Expired/missing artifacts or incomplete gates stop before writes. Inspect the
exact failed gate; if otherwise valid evidence genuinely expired or is missing,
explicitly run one read-only **CI** refresh on the same current master SHA.
There is no hidden matrix fallback or cross-SHA package-equivalence shortcut.
Artifacts retain the bounded 14-day policy.

The independent candidate-ZIP upgrade guard remains: `release.py baseline`
fetches the actual frozen stable tag into a validated baseline bundle without
moving a branch, then `upgrade_smoke.py` proves the candidate preserves user
data in fresh offline Core processes. This is not the full support matrix or
post-publication HACS download proof. First-release mode records the absence
of an older baseline rather than fabricating an upgrade.

## Publish deliberately

Run the manual **Release** workflow from `master` for the accepted version and
full SHA. Its default is a read-only evidence/upgrade audit: it makes no writes
and does not assert publication eligibility, so existing-version rehearsals are
safe. Publishing separately requires the explicit publish input, the configured
release approval environment and strict fresh tag/version/state checks.
Only accepted-master checkout code receives a release write token.

The publication gate checks the current default-branch SHA, synchronized version,
required checks, existing release/tag state and version ordering. An existing tag
or release is not overwritten. A draft receives the exact ZIP/checksum/provenance;
download the uploaded bytes and compare their hashes before publishing. Resolve
the actual tag object to the accepted commit: `targetCommitish` alone is not proof.

If a write times out or its outcome is unknown, inspect the repository, tag,
draft and assets before acting. Do not blindly rerun, delete tags/drafts, replace
assets, retarget a tag or pass a clobber flag. An unexpected partially completed
release requires a maintainer decision based on its observed state.

Published versions/assets are immutable by project policy. Enable GitHub's native
immutable-release protection when available and verify the effective setting;
policy text alone is not platform enforcement. Correct a published defect with a
new patch release and a clear advisory/migration note, not an asset replacement.

## Acceptance after publication

Use disposable isolated Core/HACS, not a live appliance installation:

1. Add the public repository as category Integration. The bootstrap source-tree
   check and the final ZIP check are distinct observations.
2. Install the published stable version through real HACS; restart the isolated
   Core, compare installed member hashes, and exercise synthetic TCP discovery,
   config entry loading and profile persistence.
3. Exercise an upgrade fixture and confirm entity/statistic identity, preferences
   and saved profiles. A source download or passing pytest alone is not proof of
   HACS installation, restart or upgrade.
4. Check release URL/tag/version, downloadable asset bytes and README links.
   Enable Release/Downloads badges only once their release/asset exists. The
   Downloads badge counts `uplus_reborn.zip`, not checksums or unique users.
5. Record tested Core/HACS versions and remaining limits. Never mark hardware or
   current/latest compatibility beyond the evidence actually obtained.

The ordinary `ha_smoke.py` network guard allows loopback only. It cannot perform
GitHub-backed HACS installation. That acceptance needs a separately controlled
HACS environment with bounded GitHub egress, synthetic loopback appliance state
and no real appliance/account credentials. It does not authorize live operations.

### Repeatable published HACS acceptance

From a clean checkout on Windows or Linux, with Docker and authenticated GitHub
CLI read access available, run:

```sh
uv run --no-project --python 3.13 python tools/hacs_acceptance.py --repository Zuz666/uplus-reborn --tag v1.0.1 --baseline v1.0.0 --output build/hacs-1.0.1
```

Use a new output directory for every attempt. This command never publishes,
changes tags or touches live HA. Historical target tags need not equal current
`master` or latest; add `--expect-latest` only for a new release's acceptance.
For the first-ever release, explicitly replace `--baseline` with `--first-release`;
that mode proves fresh installation/restart, not an unavailable prior upgrade.
Select a non-default client with `--docker-bin` or `UPLUS_DOCKER_BIN`, matching
the delivery tool's portable executable-path convention. Client lookup failure
is recorded before any GitHub download; an empty `PATH` is not a portable test
of executable absence on Windows.

The runner pins official HACS bytes in `tools/hacs-fixture.json` and uses the
digest-pinned `core-current` Docker target. Downloaded release members must match
the immutable tag's actual Git tree, not just its checksum/provenance claims.
It performs real HACS ZIP installation, fresh Core boot/persistence restart and
an actual baseline-to-target upgrade. Shared `upgrade_smoke.py phase` workers
verify identity, preferences, profiles, units, history and statistics in fresh
Core processes. Installer HTTPS is bounded to GitHub; runtime traffic is synthetic
loopback only. This is not account-onboarding or physical-appliance proof.

`acceptance.json` and per-phase receipts retain versions, exact members, duration,
failures and cleanup. Docker/network/rate-limit failures are not acceptance.
Only ephemeral repository-scoped Actions tokens may enter the container; local
installer workers use anonymous public access and need no personal token.
The ordinary offline tooling tests never download HACS or contact GitHub.

### Hosted acceptance and release outcome

Use **Published HACS acceptance** in Actions for a repeatable read-only run:
provide the existing target/baseline tags, or explicitly choose first-release
mode. Do not rerun the publisher merely to repeat acceptance. Historical runs
leave the latest assertion disabled. Per-attempt sanitized receipts are retained
as `hacs-acceptance-RUN_ID-ATTEMPT` artifacts.

The Release workflow freezes the greatest older published stable baseline before
publication and calls the same read-only workflow afterward, with latest
selection required. The acceptance job has no release write permission. A failed
post-publication check means **published, acceptance failed or blocked**; assets
remain immutable and the release work item stays open. The workflow summary
distinguishes that state from accepted, read-only preparation and an uncertain
publication. Preserve receipts and inspect actual state before any further write.

Licensed under [CC BY 4.0](LICENSE). Attribution: U+ Reborn contributors,
[U+ Reborn repository](https://github.com/Zuz666/uplus-reborn).
