# Contributing to U+ Reborn

Keep contributions evidence-based and specific to HWD120-B1558U. Implemented
controls may use inspected exact-target source encodings and deterministic
offline contracts before every program or combination has been tested on
hardware. Distinguish implementation support, source evidence, captured bytes
and physical observations; never describe source-backed behavior as universally
hardware-verified or substitute guessed encodings for missing evidence.

## Repository map and integration identifiers

Neither a checkout directory such as `haier-uhome-reborn` nor the public
repository name defines the Home Assistant domain. Never derive package paths,
imports, entity domains, or logger names from either.

- Home Assistant domain: `uplus_reborn`
- custom integration package: `custom_components/uplus_reborn/`
- canonical Home Assistant metadata:
  `custom_components/uplus_reborn/manifest.json`
- coordinator: `custom_components/uplus_reborn/coordinator.py`
- coordinator logger: `custom_components.uplus_reborn.coordinator`
- onboarding-only native UDP collection: `local_discovery.py`; stock authenticated
  form preview/static image: `discovery_preview.py`, `static/discovery.svg`;
  immutable offline category/model-family facts and provenance: `discovery_catalog.py`; existing runtime
  MAC/DHCP helpers: `discovery.py` (no new UDP recovery loop)
- embedded protocol module: `custom_components/uplus_reborn/protocol/`
- pure typed intent validation/encoding: `protocol/_commands.py` inside that package
- reported-setting applicability: `reported_settings.py`; status platforms:
  `sensor.py`, `binary_sensor.py`; cached health: `system_health.py`
- native controls: `select.py`, `switch.py`, `button.py`, `text.py`; atomic action: `services.py`
- saved profiles: `profiles.py`; persistence/selection/automation behavior:
  `tests/test_profiles.py`
- canonical English UI strings: `custom_components/uplus_reborn/translations/en.json`
- tests: `tests/`
- reusable public development scripts: `tools/`
- public user/protocol documentation: `README.md`, `CONTRIBUTING.md`, `docs/`
- Python dependencies and reproducible resolution: `pyproject.toml`, `uv.lock`
- documentation-tool dependencies and commands: `package.json`, `package-lock.json`
- automated checks: `.github/workflows/validate.yml`, `.github/workflows/docs.yml`

The manifest is the canonical source for the integration domain, display name,
version, dependencies, discovery declarations, documentation URL, issue
tracker, and Home Assistant integration metadata. Keep this map synchronized
when moving code, but do not duplicate mutable manifest values here.

Declare catalog categories in the manifest using the
[official taxonomy](https://contributors.home-assistant.io/core/integrations/categories/).
Classify the appliance's purpose, not its entity platforms; the HACS repository
category is a separate setting. Fix missing-category warnings rather than
suppressing the metadata check.

## Development and verification

Use `uv` to manage the Python environment and dependency lockfile. The repository's **Developer commands** section in [README.md](README.md#developer-commands) contains the current setup, test, lint, protocol-CLI, and documentation-check commands.

Before proposing a feature or bug fix, run the specific verification that demonstrates the changed behavior. Select the smallest relevant check first, then run any broader affected checks required by the repository's existing conventions. For changes that need live hardware evidence, describe the safe, reproducible observation in the pull request without publishing sensitive data. Do not commit incomplete or unverified work merely to create a boundary.

### Independent development

A clean public checkout must be sufficient to install dependencies, run the
documented checks, and contribute a change. Private maintainer workspaces,
agent instructions, research skills, captures, accounts, and live appliances
are not prerequisites. Keep the necessary development instructions here and
in README rather than in an ignored file.

Use the frozen-lockfile commands in README for ordinary development. When a
dependency change is intentional, update its manifest and lockfile together
and verify the affected environment. Python uses `uv`; documentation tooling
uses npm. Do not install APK-analysis or capture dependencies into integration
runtime requirements.

### Branches, issues and review

`master` is the only persistent public branch. Use short-lived branches and pull
requests for one coherent change. Squash merges use a Conventional Commit PR
title; merged branches are deleted. Do not push directly to protected `master`,
force-push release tags, or bypass required checks.

Use the public issue forms for bug, feature and compatibility evidence. A new
model report does not establish support or authorize appliance controls.
Include the integration and Core versions; HACS version and installation method
are optional context, not prerequisites for reporting a problem.
Maintainer work items state scope/non-goals, evidence, observable acceptance,
test layer, dependencies and migration/release impact before becoming Ready.
An epic links child outcomes rather than duplicating their implementation text.

Issues own public work; milestones identify concrete release boundaries; labels
classify type, priority and one/two affected areas. The Project lifecycle is
Inbox → Backlog → Ready → In progress → In review → Done. Do not duplicate those
states with normal-progress labels or maintain a second catalogue of issue bodies.
Close an issue only when its acceptance is met and its change is merged, or use
an explicit native not-planned/duplicate reason. Private plans and evidence are
not public contribution prerequisites.

The sole maintainer records self-review without requiring an impossible approval
of their own PR. Required checks and resolved conversations still apply to
administrators. Additional independent approval can become required when another
maintainer is available. Automated review is advisory, not a replacement for
review or evidence.

#### Pull request labels

[Classify pull requests](.github/workflows/pr-labels.yml) fills missing `type:*`
and `area:*` groups on open, edit, push, reopen, readiness and manual label
changes, including draft PRs. It uses only labels declared in
[the catalogue](.github/labels.json) and already present in the repository;
[Synchronize labels](.github/workflows/labels.yml) owns catalogue provisioning.

Exactly one native closing issue in this repository supplies its existing
classification. With no such unambiguous source, a Conventional Commit title
provides the type: `fix` → bug, `feat` → feature, `docs` → docs, `test` → test,
`refactor`/`perf` → refactor, and `build`/`ci`/`chore`/`style`/`revert` → chore.
Changed paths supply areas. Accompanying tests/Markdown do not inflate the areas
of implementation changes; test-only changes use testing. Rename paths count
on both sides. More than two inferred areas, conflicting labels within the source
issue, unknown paths/types and incomplete file data require manual classification;
the workflow logs what it cannot infer rather than picking an arbitrary label.

Existing label groups win. The action only adds labels: it never removes or
replaces manual labels, infers priority or adds lifecycle/special labels.
This deliberately does not continuously synchronize an already labeled PR
after its scope or issue labels change. To correct it, apply the intended
labels manually or clear the affected group and rerun **Classify pull requests**
from `master` with its open PR number. Inspect the result before merge.

The privileged job uses `pull_request_target` so fork PRs can be labeled, but
executes only the trusted base-commit module/catalogue. No PR-head code,
dependency install, cache, artifact, PAT or repository secret is consumed.
PR text is API data, never interpolated into shell/JavaScript. The token has
only contents/issue read and PR write permissions; API failures remain failed
runs. Runs serialize per PR and re-read metadata before adding labels. This
reduces stale decisions but is not an atomic lock against simultaneous human
edits. Fork execution and required CI protections are not relaxed.

Where GitHub's Actions event policy restricts `pull_request_target`, permit only
this workflow's target/dispatch events, not the event repository-wide. See
[GitHub's security guidance](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target).
Run `npm run test:labels` with Node.js 22+; CI exercises Windows and Linux.
CodeRabbit labeling remains disabled and is not a dependency of this process.

#### CodeRabbit review

[`.coderabbit.yaml`](.coderabbit.yaml) owns the repository review configuration.
CodeRabbit uses this contribution contract and the security policy as guidelines,
with focused path instructions rather than a second copy of every project rule.
Reviews use English, the balanced `chill` profile and advisory feedback; required
CI and maintainer review remain authoritative. Automatic review targets the
default `master` branch, skips drafts and pauses after two reviewed commits.
Dependency PR authors are not excluded, and lockfiles remain reviewable.

Service eligibility and rate limits can still prevent automatic review; YAML
does not override an account or OSS-plan restriction. On a ready PR, comment
`@coderabbitai review` for new changes, `@coderabbitai full review` for a deliberate
fresh pass, or `@coderabbitai configuration` to inspect the effective settings.
Use explicit mentions for follow-up questions; automatic chat replies are off.

Evaluate each finding against code and evidence. Fix valid findings, or explain
why they are obsolete, incorrect or out of scope before resolving the discussion.
Do not treat generated approval or a skipped review as test evidence. Configured
code-writing finishing touches, issue enrichment and automatic labeling are off.
Do not enable Autopilot/Autofix or request bot-authored commits as part of ordinary
review; those require their own explicit scope. Repository YAML is not a GitHub
App permission boundary and does not claim to disable every service-side agent.
Context is repository-local; no other repository, Jira, Linear or MCP is linked.
Never submit credentials, APKs, raw captures or private workspace evidence to chat.

For configuration changes, validate against the
[official schema](https://www.coderabbit.ai/integrations/schema.v2.json) and inspect
the bot's actual response on the PR. See the
[configuration reference](https://docs.coderabbit.ai/reference/configuration),
[automatic-review controls](https://docs.coderabbit.ai/configuration/auto-review)
and [guideline ingestion](https://docs.coderabbit.ai/knowledge-base/code-guidelines).

### Dependency updates

Dependabot proposes weekly uv, npm, GitHub Actions and Docker updates. Keep
manifest/lock changes together; frozen installs must resolve both the separate
research and test groups. Major updates and changes to Core/pytest compatibility
baselines require separate review, not automatic grouping with routine tooling.
No dependency PR is automatically merged.

The test group's DHCP, network and frontend pins belong to the minimum Core's
component requirements. Update them with the Core/pytest baseline, not as an
unrelated tooling upgrade. Newer versions in the separate research resolution
do not change these frozen minimum-environment requirements.

Ruff upgrades must not silently reduce lint coverage. The datetime checks
removed from upstream defaults in 0.17 remain explicitly enabled in the
project configuration.

External Actions and required container bases are pinned by immutable SHA/digest.
Review upstream changes and the effective runtime, not only the version comment.
Required minimum/current Core baselines change through an explicit PR; moving
stable/beta/dev canaries report drift separately and do not redefine support.

Workflow bootstrap pins uv and Python explicitly; a `setup-uv` major update
does not update either runtime. Preserve its sensitive-event cache protections
instead of enabling dependency caches under privileged triggers.

Internal tooling updates normally need no user-facing release note; changed
contributor prerequisites do. See [SECURITY.md](SECURITY.md) for vulnerability
reporting and [the release runbook](docs/release-workflow.md) for publishing.

### Integration translations

Edit `custom_components/uplus_reborn/translations/en.json` directly. It is the
single source of English UI strings and the file Home Assistant loads at
runtime. Custom integrations ship complete translation files; they do not use
the Core-only `strings.json` compilation workflow.

Do not add a duplicate `strings.json`, a translation-generation step, or a
symlink in place of the JSON file. Keep translations as ordinary files so
Windows/Linux checkouts and installation archives behave consistently.

The complete Russian locale is shipped in `translations/ru.json`. Keep its key
structure and interpolation placeholders aligned with `translations/en.json`.
Translate displayed wording, never runtime option values or automation keys.

Use the [selected HWD120-B1558U base terminology](docs/terminology/hwd120-b1558u.md#selected-base-terminology)
for machine wording; the source tables retain manual/APK alternatives as evidence,
not interchangeable UI choices. Use **programme** in displayed English wording;
existing API and translation keys remain unchanged. The reference is not a
translation catalogue or proof of wire encodings. Do not present software-only
concepts as verbatim manual terminology.
Keep machine terminology references in `docs/terminology/<model>.md`, one file
per model. This organization does not establish integration support for another
model; each reference needs its own model-specific source evidence.

The [entity-name register](docs/terminology/hwd120-b1558u-entity-naming.md) records compact names in both
locales and their abbreviation rules. Preserve its deliberate U+200A grouping
spaces and visible draft/readback distinctions when changing names; update both
locales and the register together without changing identities or user overrides.

Validate changes with Hassfest and inspect the affected native HA surface.

HA state translation keys must be lowercase and match the entity's exact
option values. Smart fabric/stain selectors convert source recipe IDs to
lowercase only in `select.py`, then convert selected Smart values back for
typed drafts. Keep capability tables and the atomic action's uppercase source
IDs unchanged; do not introduce a second mapping table.

### Public tooling boundary

Reusable development scripts belong in `tools/`. Package-level commands may
invoke those scripts or installed dependency executables; dependency manifests,
lockfiles, and CI workflows stay at their normal public locations. Public tools
must not import, execute, or discover files from a private workspace or rely on
root agent-instruction links.

Support Windows and Linux for shared documentation tooling, with explicit
paths and argument handling rather than shell-specific glob expansion. Keep
the same commands and file-selection rules locally and in CI. The HA Python
test suite runs in the reproducible Docker Linux container (`tools/Dockerfile`),
not a reason to make other tools platform-specific.

Current public tool inventory:

- `tools/generate_brand_icon.py` regenerates the checked-in Haier Duo
  brand icons (`icon.png` at 256px and `icon@2x.png` at 512px). It is optional
  and not required for tests or installation. From the root on Windows or Linux,
  run `uv run --no-project --with pillow==12.0.0 python tools/generate_brand_icon.py`.
  The standalone command pins Pillow for reproducible rendering without
  installing Home Assistant or research dependencies.
- `tools/release_evidence.py` produces CI candidate/matrix receipts and selects
  or audits authenticated exact-master source evidence. See
  [the handoff contract](docs/release-workflow.md#trusted-master-consumption);
  local JSON success and PR artifacts never authorize publication.
- `tools/hacs_acceptance.py` runs explicit real published HACS installation and
  persistent-data upgrade acceptance from a clean Windows/Linux checkout with
  Docker. Its pinned fixture and shared Core phase require no private workspace.
  See [the release runbook](docs/release-workflow.md#repeatable-published-hacs-acceptance);
  this networked scenario is separate from ordinary offline tests.
- `npm run test:docs` runs `tools/check-docs.mjs`: Markdown lint plus strict
  Mermaid validation over one sorted, public-only file selection. Both checks
  run regardless of the first result and never mutate documents.
- `npm run test:docs:tooling` exercises real linters and the runner's selection,
  nested-fence handling, LF/CRLF preservation, portable paths, symlink exclusion
  and failure propagation using disposable fixtures.

Documentation tooling requires Node.js 22 or newer; CI covers Node.js 22 on
Windows and Ubuntu. Use `npm ci --ignore-scripts` to install the locked tools.
The default Markdown rules are enabled, except hard line length (`MD013`) and
table-column alignment (`MD060`), to preserve the existing prose and compact
protocol tables. The independently written `tools/closed-fences.cjs` rule checks
explicit closure using parser tokens rather than line-prefix guesses.

The Mermaid CLI receives normalized diagram fences over stdin, extracted with
the Markdown parser pinned by `markdownlint-cli2`. This covers nested containers
and avoids treating examples inside other code blocks as diagrams. Output maps
back to original filenames and line numbers. Do not replace this with a broad
glob or a private helper, suppress failures to make CI green, or add automatic
diagram rewriting. No fix command is provided; review source edits explicitly.

### Choosing code and verification scope

- Protocol framing, decoding and lifecycle confirmation belong in the embedded
  protocol package, with offline cases in `tests/test_protocol.py` and
  `tests/test_response_contract.py`.
- Pure intent validation/encoding belongs in `protocol/_commands.py`, with
  source/capture-derived behavioral cases in `tests/test_commands.py`.
- Coordination, per-drum draft isolation, atomic actions, opt-in and HA lifecycle
  belong in the integration layer, with cases in `tests/test_integration.py`.
- Saved profiles persist complete typed intents in entry-bound versioned HA
  storage (`profiles.py`). Per-drum `text.py` name inputs and Save buttons create
  profiles on the native device page; selectors restore drafts locally.
  Name inputs and drafts are transient, while saved records survive reload.
  `start_profile` supplies one immutable revision to the existing atomic Start
  path. Exercise `tests/test_profiles.py` and isolated `--scenario profiles`,
  plus the changed native UI surface. Do not migrate profiles by filling missing
  fields with current source defaults or restore drafts after reload.
- Setup/options behavior belongs in `config_flow.py` and
  `tests/test_config_flow.py`; source-profile constraints have
  `tests/test_capabilities.py`.
- Native LAN discovery/catalog behavior belongs in `local_discovery.py`,
  `discovery_preview.py`, `discovery_catalog.py`, `config_flow.py` and
  `tests/test_local_discovery.py`. Collection belongs to the authenticated
  flow-local preview subscription, not form polling or a real entity. The
  real-Core `--scenario discovery` uses `tools/discovery_fixture.py` with loopback
  UDP/TCP peers, real adapter selection, both reply-port paths, indefinite search
  across the former 120-second boundary, explicit stop and local/manual creation.
  Its synthetic network inventory does not prove physical broadcast egress.
  The narrower `--scenario discovery-identification` exercises real UDP singleton/
  shared-model/category-only/unknown observations, compact cards and technical
  details/back navigation, preserving identities and proving no discovery sends,
  TCP enrichment or entry changes during browsing. It does not repeat the
  original 122-second collection campaign. All model responders are synthetic.
  Update display consumers together; never collapse a family to its first name
  or derive support/entry identity from catalog display metadata.
- Reported-setting applicability and external-start readback belong in
  `reported_settings.py` and the status platforms, with behavior coverage in
  `tests/test_reported_settings.py`. Keep current reported values separate from
  next-run drafts; document fields that have no established readable meaning.
- Phase-aware execution-state sensors belong in `sensor.py`; lifecycle/unknown/
  offline presentation coverage belongs in `tests/test_execution_state.py`.
  Keep execution separate from retained program phase and native button history.
- Pause/Resume rejection presentation uses structured protocol reasons in
  `entity.py`; `tests/test_lifecycle_errors.py` exercises HA services against a
  loopback UWT peer. Keep state gating, fresh validation and unknown outcomes
  unchanged when improving user-facing messages.
- Entities consume typed snapshots and intents, not raw packets. Implement
  exact-target source-backed options without claiming physical validation.
  Missing encodings are explicit gaps, not guessed values or new raw-write APIs.
- Keep fixtures minimal, sanitized, deterministic, and independent of the
  maintainer's local files. Preserve external behavior and safety boundaries,
  rather than pinning log wording or implementation details.

Choose the finite acceptance set from the changed behavior, then run it once
after related edits settle. Every change needs scope/sensitive-data review and
synchronized affected contracts; not every change needs the full product suite.
Prose-only edits need documentation/link checks, not HA startup or live access.
Protocol changes need correlation/constraint/failure evidence; native UI changes
need the affected actual UI path. Recorder/statistics checks apply when identity,
units, state classes or history change. Full release/HACS/hosted-CI acceptance is
a release concern, not an ordinary-fix loop.

Reuse successful evidence for unchanged inputs, scenario and relevant environment.
Preserve the original record and its limitations. Reopen a check for changed
inputs or contradictory evidence, not a report or phase transition. CI still
runs Hassfest and HACS repository validation. Hardware evidence and deployment
authorization are separate; no ordinary check may require an appliance.

See [reusable artifacts and isolated Core](docs/development-verification.md) for
the public `tools/delivery.py` and `tools/ha_smoke.py` interfaces, exact-byte
packaging, explicit-input receipts, and reproducible Docker runtime setup.
These tools have no private workspace or live-hardware dependency.

## Scope changes and commits coherently

A commit is not defined by a line count or number of files. Each commit must be independently coherent and verified: it should make one feature, bug fix, documentation change, policy change, or release change understandable and usable on its own.

After a coherent feature or bug fix has passed its specific verification, make the local commit before beginning unrelated work. Keep features, bug fixes, and policy or release housekeeping in separate commits. A single feature or bug-fix commit may include its implementation, every affected callsite, contract tests, and the documentation needed to keep that one concern internally consistent. It must not include a second, unrelated concern.

Immediately before a feature or bug-fix commit, synchronize every affected source of truth:

- implementation and all affected callers;
- tests that define the changed observable contract;
- affected user documentation (`README.md`) and contributor module/verification
  contracts (`CONTRIBUTING.md`);
- `docs/protocol.md` **only** when appliance wire protocol, framing, state-word
  coordinates, command encodings, or hardware/source protocol evidence changes
  (never record Home Assistant entities, staged drafts, saved profiles,
  translation keys, service handlers, Recorder internals, or coordinator classes
  in `docs/protocol.md`); and
- any related local research records or analyses.

Update protected local research records when relevant, but keep them out of
public Git history and public tool dependencies. A maintainer may version an
isolated private workspace separately; never force-add it to the public project.

## Commit messages

Use the Conventional Commits form:

```text
<type>(<scope>): <imperative subject>
```

- **type** is lowercase and describes the change: `feat`, `fix`, `docs`, `test`, `refactor`, `build`, `ci`, or `chore`.
- **scope** is a short, lowercase, hyphenated name for the affected area. Omit it only when no useful area applies.
- **subject** is a concise imperative description: start with a verb, use lowercase, do not end with a period, and do not restate the type or scope.

Examples:

```text
feat(protocol): expose verified remaining-cycle time
fix(config-flow): reject duplicate local setup
docs(readme): clarify remote-control opt-in
test(entities): cover cumulative sensor reset
refactor(coordinator): isolate state snapshot conversion
chore(deps): refresh the locked development dependencies
```

Use a `!` after the type or scope, and explain the migration in the commit body and pull request, when a change is breaking:

```text
feat(entities)!: rename the cycle counter entity
```

## Versions and releases

The integration uses Semantic Versioning: `MAJOR.MINOR.PATCH`.

- For a stable `1.0.0` or later release line, increment **MAJOR** for an incompatible public change that requires users to adapt configuration, automations, entity usage, or another supported interface.
- Increment **MINOR** for a backward-compatible, user-visible feature.
- Increment **PATCH** for a backward-compatible bug fix.
- Before `1.0.0`, use a **MINOR** increment to signal user-visible additions or incompatible public changes, and reserve **PATCH** for backward-compatible bug fixes.

Do not change the integration version in ordinary feature or bug-fix commits. A version bump belongs only in a release commit. In that release commit, set the same version in `pyproject.toml` and `custom_components/uplus_reborn/manifest.json`, then refresh and commit `uv.lock` so its root package entry matches. Create or update `CHANGELOG.md` in the same release commit. The changelog entry must include the version, release date, user-visible features, fixes, and breaking changes. Do not create a changelog entry until preparing an actual release.

The first public release is **1.0.0**. Unpublished development versions are not
historical public releases. Initial public history starts from a reviewed clean
snapshot; subsequent development follows the ordinary PR workflow above.
The release runbook defines version validation, committed-byte ZIP packaging,
manual approval, immutable tag/asset handling and actual HACS acceptance.

Use the [portable release commands](docs/release-workflow.md#portable-status-advisory-delta-and-terminal-report)
for baseline selection, exact-run status, advisory inventory/delta and the final
report; do not author a new private probe for each release. `release.py prepare`
updates synchronized version metadata and curated notes without resolving
dependencies, committing or publishing. Preserve the prior advisory inventory:
unchanged accepted risks are not new findings, and unavailable access is not
zero alerts. Approval, source verification and immutable publication stay separate.

## Private material and security

Never commit secrets or sensitive artifacts. This includes credentials, tokens, personal data, device-specific identifiers, proprietary application materials, decompiled content, and unredacted network captures or diagnostics. Redact issue and pull-request evidence before posting it. Keep private or ignored artifacts local, and never use force-add to make them tracked.

Do not turn uncertain observations into protocol claims. Public documentation and commit messages must distinguish verified behavior from investigation.

## Pull requests

A pull request should make review straightforward:

- state the user-visible problem and the one concern addressed;
- link the relevant issue when one exists;
- summarize the verification actually performed and its result;
- identify affected public documentation, tests, and sources of truth; and
- call out breaking changes, migration steps, and release notes when applicable.

Keep unrelated formatting, refactors, dependency work, policy updates, and release housekeeping out of the pull request unless they are the pull request's declared concern. Resolve review feedback with the same coherence and verification standard used for commits.
