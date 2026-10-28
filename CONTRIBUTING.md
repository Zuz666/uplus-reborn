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
- onboarding-only discovery lifetime: `discovery_interval.py`; shared native UDP
  collection: `local_discovery.py`; authenticated stock preview presentation:
  `discovery_preview.py`, `static/discovery.svg`; immutable offline category/model
  facts and provenance: `discovery_catalog.py`; existing runtime MAC/DHCP helpers:
  `discovery.py` (no new UDP recovery loop)
- embedded protocol module: `custom_components/uplus_reborn/protocol/`
- pure complete-intent preparation: `prepare_intent` in `protocol/_commands.py`;
  draft editing: `update_intent` in the same module; existing constructors,
  typed validation and encoding remain there
- reported-setting applicability: `reported_settings.py`; status platforms:
  `sensor.py`, `binary_sensor.py`; cached health: `system_health.py`
- bounded opt-in diagnostic storage: `exchange_trace.py`; finite capture/logger
  ownership: `capture_lifecycle.py`; normalized budgets/depth: `capture_options.py`;
  protocol observation contract: `protocol/_observation.py`; runtime operations: coordinator
- native controls: `select.py`, `switch.py`, `button.py`, `text.py`; atomic action: `services.py`
- saved-profile operation policy/completion: `coordinator.py`; durable store:
  `profiles.py`; native action choices: `profile_choices.py`; consumer contracts:
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

### Model-specific protocol documentation

The [protocol index](docs/protocol.md) links specifications under
`docs/protocol/<model>.md`; the verified target is
[HWD120-B1558U](docs/protocol/hwd120-b1558u.md). Keep the model and applicable
source/firmware scope explicit. For another model, add a separate reference and
index entry with its own evidence; a document or shared port does not establish
integration support. Extract a common protocol reference only when independent
model evidence establishes that the contract is actually shared.

Keep wire layouts, message/field semantics, source anchors, sanitized captured
examples and unresolved meanings in these files. Exclude internal campaign
labels/dates, deployment chronology and HA-specific behavior. Distinguish complete
packets from excerpts/redactions and source-only formats; never invent captured
examples. Update navigation and PR path classification when reorganizing them.

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
Run `npm run test:labels` with Node.js 22+. PR CI runs label rules on Linux;
master/manual/unknown-context CI retains Linux and Windows. Documentation runner
regressions and public tooling remain required on both platforms for every PR;
only repository Markdown/Mermaid content validation is Linux-only on PRs.
See the [CI platform policy](docs/development-verification.md#ci-platform-coverage).
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

Support Windows and Linux for shared tooling, with explicit paths and argument
handling rather than shell-specific glob expansion. Keep the same standard
scope and repository configuration locally and in CI. Native local Ruff via
`python tools/delivery.py lint` is the narrow exception to Docker-based Python
verification: it checks working-tree files under `custom_components`, `tests`
and `tools`, selecting the exact Ruff from `uv.lock` through uv's isolated tool
cache rather than a mutable project `.venv` or a global installation.

Native Windows Ruff does not enforce EXE001/EXE002; its success is local feedback,
not executable-permission proof. Canonical Linux CI/container lint remains
required before merge. The entrypoint is read-only for source files and the Git
index, permits ordinary tool caches, and does not forward arbitrary Ruff
arguments or provide fixing/formatting. See the
[public verification contract](docs/development-verification.md#native-local-ruff-windows-and-linux)
for version-selection failures and evidence limits.

Run `python tools/delivery.py lint` before the standalone tooling suite to
bootstrap the locked uv tool cache. Native CLI safety regressions then run
offline against real Ruff, including hostile configuration/environment cases.

HA pytest, POSIX checks and real Core still run in reproducible Docker Linux
containers (`tools/Dockerfile`). If required Docker is unavailable, stop those
checks; native Ruff remains usable. WSL and mutable project `.venv` environments
are not alternative execution paths.

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
  Validate each state frame into one complete word sequence, then reuse it for
  snapshot projection and any selected-frame raw inspection. This boundary does
  not change stream queues, report retention, correlation or confirmation proof.
- Temporary exchange tracing observes only existing runtime entry traffic.
  The coordinator owns per-entry operation grouping; `capture_lifecycle.py` owns
  finite timers, explicit start/reload handoff and domain-wide native DEBUG
  ownership. Guard synchronous as well as deferred native logger notifications.
  The protocol emits passive observations without changing validation/proof.
  `exchange_trace.py` enforces selected operation/event/raw-byte limits and bounded
  metadata; `capture_options.py` normalizes finite preferences and estimates
  ordinary-poll depth independently of the deadline. Existing DEBUG at setup is
  not consent to resume capture. Keep incoming-envelope processing independent
  of diagnostic retention.
  Ordinary logs/state/config remain redacted; only the explicit opt-in trace
  section may include original local bytes/endpoints/device identifiers.
  Never include onboarding/cloud secrets or unrelated traffic. Use synthetic
  data in tests and review evidence, not committed real traces.
- Pure intent validation/encoding belongs in `protocol/_commands.py`, with
  source/capture-derived behavioral cases in `tests/test_commands.py`.
- Coordination, per-drum draft isolation, atomic actions, opt-in and HA lifecycle
  belong in the integration layer, with cases in `tests/test_integration.py`.
- Saved profiles persist complete typed intents in entry-bound versioned HA
  storage (`profiles.py`). Per-drum `text.py` name inputs and Save buttons create
  profiles on the native device page; selectors restore drafts locally.
  Coordinator operations own applicable draft snapshots and post-commit listener/
  native-choice updates; button/action callers do not arrange that sequence.
  `profile_choices.py` owns labels and cross-entry action descriptions, also
  refreshed on entry setup/unload. A failed commit publishes no mutation;
  presentation failure after commit does not roll back or replay storage.
  Name inputs and drafts are transient, while saved records survive reload.
  `start_profile` supplies one immutable revision to the existing atomic Start
  path. Exercise `tests/test_profiles.py` and isolated `--scenario profiles`,
  plus the changed native UI surface. Do not migrate profiles by filling missing
  fields with current source defaults or restore drafts after reload.
- Setup/options behavior belongs in `config_flow.py` and
  `tests/test_config_flow.py`; source-profile constraints have
  `tests/test_capabilities.py`.
- Native LAN discovery/catalog behavior belongs in `discovery_interval.py`,
  `local_discovery.py`, `discovery_preview.py`, `discovery_catalog.py` and
  `config_flow.py`. Exercise interval ownership through its production interface
  in `tests/test_discovery_interval.py`, with real loopback UDP; native flow and
  presentation contracts remain in `tests/test_local_discovery.py`.
  The interval owns tasks, owner subscriptions, frozen observations and cleanup;
  the flow owns catalog history/navigation, and the preview renders observations.
  The preview adapter must register pending connection cleanup before asynchronous
  initialization; a disconnected attempt cannot acquire a late search owner.
  Explicit Stop settles final publication to connected subscribers before
  releasing them. HA shutdown consumes its one-shot listener before the callback;
  cleanup must not invoke the already-consumed unsubscribe handle again.
  Collection belongs to the authenticated flow-local preview subscription, not
  form polling or a real entity. The collector keeps each active search's
  observations and activity in one registration, sharing one immediate/20-second
  send loop and sockets. Joining does not send or reset the schedule. Sent/Rounds
  describe shared submissions/rounds during that interval's participation; Replies
  includes every valid datagram seen in that window, without query attribution.
  Devices remain deduplicated by identity. Last-owner cleanup settles the sender
  before admitting a fresh run. Fatal sender termination freezes each active
  interval as failed while preserving its own accepted records and diagnostics;
  recoverable coverage warnings do not add retries or change cadence. The
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
- model-specific `docs/protocol/<model>.md` references **only** when appliance
  wire protocol, framing, state-word coordinates, command encodings, or
  hardware/source protocol evidence changes (never record Home Assistant
  entities, staged drafts, saved profiles, translation keys, service handlers,
  Recorder internals, or coordinator classes in protocol specifications); and
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

Do not change the integration version in ordinary feature or bug-fix commits.
A version bump belongs only in a release commit. Run
`python tools/release.py prepare --version VERSION --date YYYY-MM-DD --notes PATH`
to synchronize manifest/project/root-lock versions and the dated changelog
section, then run `uv lock --check --offline` and review the diff. Commit these
files together. Notes must describe user-visible features, fixes and breaking
changes. Do not create a changelog entry until preparing an actual release.

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
