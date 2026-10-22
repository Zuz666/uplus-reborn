# Development verification and reusable artifacts

Use the smallest acceptance set that covers the changed behavior. Normal checks
never contact an appliance; physical evidence and deployment permission are
separate from software verification. Start with [CONTRIBUTING](../CONTRIBUTING.md)
for the module map and the [README commands](../README.md#developer-commands).

## Choose checks by impact

| Change | Evidence to obtain |
| --- | --- |
| Every change | Review scope and sensitive material; synchronize affected callers/contracts; record the relevant result or why a check does not apply |
| Documentation only | Markdown, Mermaid where present and changed links; no integration test suite or HA restart |
| Python behavior | Focused behavior regression and executable smoke; affected broader tests/lint once after edits settle |
| HA API/lifecycle/identity changes | Isolated real Core with actual transport; supported-version coverage where the API seam changes |
| Native UI change | Exercise the affected actual UI surface; API state is not visual proof |
| Protocol or command change | Framing, correlation, constraints, failures/unknown outcomes and relevant capture replay; no mandatory physical Cartesian product |
| Cross-platform tooling | Actual Windows/Linux commands and applicable failure boundaries |
| Release | Full release checks, supported platforms, HACS installation/upgrade, hosted CI and release-only version/changelog |

Recorder/statistics checks apply to identity/history, units or state-class changes.
Do not rerun them for unrelated prose or command-error wording. A new report or
phase does not invalidate an unchanged successful result.

## Temporary Hassfest source build

The Hassfest job builds the official Home Assistant Dockerfile from a fixed
upstream commit because the published validator predates manifest categories.
The workflow pins the source archive by SHA-256 and overrides the recipe's Python
base by immutable digest. The official recipe and validator are not patched;
the selected source includes both categories support and uv 0.13.4.

Build-time dependency downloads are separate from validation: the validator runs
without network, with the candidate checkout mounted read-only and no credentials
passed into the container. The required Hassfest check and aggregate CI gate
remain enforced. This development-tool revision does not raise the integration's
minimum supported Core version or replace its isolated runtime checks.

The pins and commands live in [the validation workflow](../.github/workflows/validate.yml).
[Issue #27](https://github.com/Zuz666/uplus-reborn/issues/27) tracks removal of this
temporary build when a compatible official image is available. Verify that
replacement accepts the valid manifest and still rejects invalid metadata before
switching to its immutable digest; remove the obsolete build steps and guidance.

## Commit-only artifact tooling (Windows and Linux)

The standard-library CLI requires Python 3.13 or newer and Git; no Home Assistant,
private workspace or credentials. Use uv **0.13.4 or newer** for network-facing
dependency/Python downloads; older bundled certificate stores have a disclosed
compromised-CA vulnerability. `build/` is ignored by the repository.

```sh
uv run --no-project --python 3.13 python tools/delivery.py build --commit HEAD --output build/delivery-candidate
uv run --no-project --python 3.13 python tools/delivery.py verify --bundle build/delivery-candidate
uv run --no-project --python 3.13 python -m unittest discover -s tools -p "test_*.py" -v
```

`build` resolves the requested commit, not a dirty checkout. Command-local
`core.autocrlf=false` and `core.eol=lf` export committed bytes without changing Git
settings. The new output directory contains:

- `artifact.tar.gz`: only `custom_components/uplus_reborn/`;
- `artifact.json`: schema, full commit, archive SHA-256 and every regular member's
  path/hash, compared with the committed blobs.

`build` already validates the bundle. The separate `verify` example is for a
received/existing bundle or installed tree, not a mandatory immediate second pass.

Unsafe paths, links, duplicate/case-colliding names, bytecode, unexpected roots,
export-attribute omissions/substitutions and member/hash mismatches fail closed.
Existing bundle directories are never overwritten. Choose a new output path for
a new candidate. To compare an installation, add `--installed CONFIG_ROOT`; only
generated Python caches are excluded, not obsolete source files.

These checks prove packaging/integrity, not a trusted signature or appliance
behavior. Review the selected commit and its provenance separately.

## Explicit-input check receipts

`check` runs a shell-free command once and records its exit status, output,
duration, command, inputs and caller-supplied version context. A successful result
is reused only when that binding matches and inputs remained stable during the
run. Failed/interrupted/changed results are never reusable.

Example, running inside the reproducible Docker test image (built once from
`tools/Dockerfile` + `uv.lock`; if the Docker daemon is unavailable, the command
stops immediately with `DOCKER_UNAVAILABLE` and exit code 2):

```sh
uv run --no-project --python 3.13 python tools/delivery.py docker --target test --record build/protocol-check.json --input custom_components/uplus_reborn --input tests --input pyproject.toml --input uv.lock --context suite=protocol -- pytest -q tests/test_protocol.py
```

The caller owns the **complete relevant input closure**, tool/runtime/dependency
versions and scenario. Include configuration, fixtures and environment settings
that affect the result; the tool does not discover dependencies or hash arbitrary
command executables. Recursive selections exclude private/generated trees, but an
explicitly selected private path is intentional. Do not select the repository
root with its private instruction symlinks: symlink inputs are rejected. Keep the
receipt/output directory outside the selected input tree. Receipts may contain
command output: store sensitive runs privately and redact before sharing.

Same package member hashes can justify reusing runtime evidence across a
prose-only commit. Preserve the original commit/result; do not rewrite it to
appear new. An archive's compression metadata can change while its installed
files remain identical; transfer checks still bind the exact archive SHA-256.

## Real isolated Core fixture (Docker)

`tools/ha_smoke.py` boots actual Core base functionality/registries, required API
components and the integration, using a synthetic identity and a TCP peer at
`127.0.0.1`. The default peer is query-only. It does not mock the production
coordinator, entities, decoder or transport. Each run uses a fresh temporary
config and unloads/stops afterward.

Use `tools/delivery.py docker` to run against the immutable container images
(`--target core-current` for Home Assistant 2026.10.2 on Python 3.14; `--target test`
for minimum supported Core 2026.2.3 on Python 3.13). Each image is built at most
once per recipe/lockfile hash and reused without checking mutable `.venv` state.
If Docker is unavailable, stop the workflow and notify the user.

```sh
uv run --no-project --python 3.13 python tools/delivery.py docker --target core-current -- python3 tools/ha_smoke.py --bundle build/delivery-candidate --output build/ha-smoke-result.json --scenario execution
uv run --no-project --python 3.13 python tools/delivery.py docker --target test -- python tools/ha_smoke.py --bundle build/delivery-candidate --output build/ha-smoke-min-result.json --scenario standby
```

`--requirements` reads the selected Core's integration manifests and writes their
required dependency closure to a new file; it does not fetch packages. This avoids
maintaining a second hard-coded list of version-specific DHCP requirements.

The default `standby` scenario proves both drums' actual HA phase/execution state.
`execution` additionally proves Running → Paused → Running while phase stays
Spinning. It records Core/Python versions, the artifact and installed hashes,
real query counts, authoritative snapshots/entity states and shutdown evidence.
A control or malformed request fails acceptance. Output files are never replaced.

The `profiles` scenario enables controls only on that synthetic entry. It proves
per-drum native text-name → Save button → restore-selector behavior with no peer
traffic, stable native action choices, invalid ID/drum/state rejection,
independent drafts, reload persistence and a second isolated Core boot reading
the same saved profiles with empty name inputs/drafts/selections. Its peer accepts only one
independently specified upper Spin/400 grouped Start plus initialization, then
returns a correlated active report. Any different or repeated control fails.
This is protocol/runtime proof, not physical appliance acceptance.

```sh
uv run --no-project --python 3.13 python tools/delivery.py docker --target core-current -- python3 tools/ha_smoke.py --bundle build/delivery-candidate --output build/profiles-smoke.json --scenario profiles
```

The `discovery` scenario exercises real UDP collection and native config-flow
steps against loopback peers: enabled-adapter prefix calculation, continuous
20-second queries beyond the former 120-second limit, repeated ephemeral/fixed
port replies, exact counters and explicit stop without TCP enrichment,
unsupported/unknown/configured inspection, supported addition and explicitly
confirmed manual fallback with actual TCP validation. Collection starts only
after preview ownership, never merely from preparing a form.
The disposable HA network inventory is synthetic; this does not prove physical
LAN broadcast egress. `--serve` keeps the peers available for actual stock form/
preview, animation continuity, Close and WebSocket cleanup acceptance.

```sh
uv run --no-project --python 3.13 python tools/delivery.py docker --target core-current -- python3 tools/ha_smoke.py --bundle build/delivery-candidate --output build/discovery-smoke.json --scenario discovery
```

The narrower `discovery-identification` scenario covers offline model-family
presentation and the two-level device view without replaying the full collection
campaign. Ten synthetic UDP identities include a single manufacturer model,
shared-model families (including two devices of the same family), category-only,
unknown, configured and unsupported-security responders. The scenario checks
card → technical details → same card/catalog, identity-based counts, source
metadata, journal separation and zero UDP submissions/TCP enrichment/entry changes
during browsing. These are synthetic model observations, not hardware coverage.

```sh
uv run --no-project --python 3.13 python tools/delivery.py docker --target core-current -- python3 tools/ha_smoke.py --bundle build/delivery-candidate --output build/discovery-identification-smoke.json --scenario discovery-identification
```

Add `--serve` for actual native cards/dropdowns and preview lifecycle inspection
on minimum/current Core, including narrow viewports. Stock dropdowns may shorten
long labels; full identifiers and all model candidates remain in the device
card. Keep this native-layout limit explicit rather than injecting frontend CSS.

For localization acceptance, set the Core language with `--language ru` (default
`en`) **before entity registration**, and select the same frontend language
separately. Home Assistant creates entity names in the server language, while
state/option labels follow the frontend locale. An English-Core/Russian-frontend
run does not prove Russian entity names. The selected Core language is recorded
in the receipt and reused by the profiles restart scenario; no registry-name
overrides are needed.

Adding `--serve` (with optional `--serve-timeout SECONDS`, default `300`) keeps the
isolated Core serving on loopback after initial scenario assertions. Both `--bundle`
and `--output` remain mandatory. Stdin must be held open with a persistent pipe
(e.g. `stdin=subprocess.PIPE`); immediate EOF causes an early exit before UI
inspection. It creates a disposable local admin user and token, emits one JSON
readiness line on `stdout` (`event: "serving"`, `url`, `entry_id`,
`frontend_loaded`, `access_token`, `refresh_token`), accepts line-delimited
`standby`, `running`, `paused` or `stop` commands on `stdin`, and revokes the token
and removes the user before unloading the entry and writing `--output`. Clean
shutdown requires writing `stop\n` to `stdin` and waiting for completion (never
terminate host processes on Windows, as child Docker containers become orphaned).
Temporary tokens are never stored in `--output`.
The reproducible minimum-Core image includes the selected Core's frontend/
Lovelace requirement closure. A serving run refuses to claim UI readiness when
the frontend is missing; a blank HTTP page is not native UI evidence.

Python socket auditing blocks non-loopback connections, sends, binds and DNS,
and blocks child processes/raw IP sockets. It is an accidental-network-use guard,
**not an OS sandbox for untrusted Python or native extensions**. Do not run
untrusted code or provide real credentials. Core's HTTP listener is loopback-only;
no browser session, frontend visual acceptance, hardware behavior or real HACS
installation is claimed. Other feature-specific isolated scenarios remain
necessary when those behaviors change.

The aggregate `ci.yml` runs for every PR, `master` push and manual invocation. It
requires Docker HA tests/Ruff, pinned Hassfest/HACS validation, workflow/secret
checks, Windows/Ubuntu standalone tooling and documentation, advanced CodeQL,
and the finite minimum/current Core matrix. Required jobs have no path filters
that can leave protection permanently pending. The release workflow reuses this
same gate rather than maintaining a weaker parallel acceptance path.

The HACS job requires GitHub's license endpoint to identify the exact checked
commit as MIT. HACS's own license check reads default-branch repository metadata
instead, so only that duplicate check is excluded; it cannot validate a license
correction in a pull request. Other HACS checks remain enabled. The documentation
license and attribution remain separately defined by README and `docs/LICENSE`.

| Layer | Required release evidence |
| --- | --- |
| Metadata and packaging | Manifest/project/uv root agree; dated notes; committed-byte, licensed, root-layout ZIP; rejection of unsafe/incomplete/occupied candidates |
| Standalone tooling | Windows and Ubuntu artifact/release boundary regressions, including stale SHA, duplicate tag, version rollback and failed required checks |
| HA contracts | Full pytest and Ruff in the frozen Docker test image |
| Real Core | 2026.2.3 and 2026.10.2: standby, execution, profiles including restart, discovery and discovery-identification |
| Public surface | Markdown/Mermaid, actual issue/PR/private-report routes, hosted CI and effective protection settings |
| HACS | Separate actual source-tree bootstrap and published-ZIP installation/upgrade; exact installed-member comparison |

Base images are digest-pinned. Minimum-Core frontend/DHCP requirements are
resolved in the committed uv test group and checked against Core's manifests;
the image build never installs an unlocked supplemental dependency set.
Scheduled/manual stable, beta and dev Core canaries use deliberately moving
images and preserve failures as visible drift evidence. They are not required
release baselines and do not silently expand supported Core versions.

Native EN/RU UI acceptance uses Full HD (1920 × 1080, DPR 1); reuse previously
accepted evidence when names, frontend/layout and relevant environment are
unchanged. CI cannot convert API state into visual proof or software coverage
into physical program coverage. The [release runbook](release-workflow.md)
defines the separate publication and HACS acceptance gates.

## Persistent-data upgrade and ZIP acceptance

Use the current-Core image and two validated committed-byte bundles. The older
bundle supplies the installed baseline; the candidate bundle is the expected
member/hash contract for the actual release ZIP.

```sh
uv run --no-project --python 3.13 python tools/upgrade_smoke.py --help
uv run --no-project --python 3.13 python tools/delivery.py docker --target core-current -- python3 tools/upgrade_smoke.py --from-bundle build/baseline --bundle build/release-candidate/bundle --release-zip build/release-candidate/uplus_reborn.zip --output build/upgrade.json
```

The fixture creates saved profiles, user overrides/options, litre history and
hourly consumption statistics in real Core. It stops that process, validates and
extracts the candidate ZIP, replaces the complete integration directory, and
starts a fresh Core process with the same configuration/Recorder database.
Acceptance compares every registered domain/unique-ID identity, device/entry
identity, names/icons/areas/preferences, sensor units/classes, saved profiles,
historical states and statistic identity/values. Ephemeral loopback ports are
excluded from identity comparison. Output paths must be new; receipts retain
phase failures and logs, and the temporary configuration is removed.

Recorder initialization precedes entity listeners. All appliance traffic remains
synthetic loopback; this verifies package loading and data preservation, not a
HACS download or hardware behavior. Real HACS acceptance is still separate.

## Completion, not endless rechecking

A change is ready when its declared behavior is exercised, affected contracts are
consistent, evidence identifies the exact inputs/environment, and known limits
are explicit. Runtime/deployment work also binds the actual package bytes.
Reopen a check only for changed relevant inputs, a different scenario/environment
or contradictory evidence—not because another stage starts. Authorization and
fresh physical preconditions are never cached as software evidence.

Licensed under [CC BY 4.0](LICENSE). Attribution: U+ Reborn contributors,
[U+ Reborn repository](https://github.com/Zuz666/uplus-reborn).
