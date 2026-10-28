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

## Native local Ruff (Windows and Linux)

From a clean public checkout, use the Python and uv prerequisites in
[README](../README.md#prerequisites-and-clean-checkout-setup); uv **0.13.4 or newer**
is required. No private workspace, Docker, global Ruff or mutable project
`.venv` is needed:

```sh
python tools/delivery.py lint
python tools/delivery.py lint --repo PATH
```

The default repository is the current directory. The selected checkout's
working-tree `uv.lock` is the sole Ruff version source. The command uses uv's
isolated tool environment/cache with that exact version, not a Ruff found on
PATH or an unpinned latest release. Missing, ambiguous or unusable locked
selection, acquisition failures and Ruff diagnostics fail visibly; they are
not successful verification or reasons to substitute another version.

The fixed scope is `custom_components`, `tests` and `tools`, matching canonical
lint. Ruff checks the selected repository's current files and configuration
with its normal discovery/exclusion behavior: unstaged edits, new non-ignored
files and deletions are reflected on disk, regardless of HEAD or index contents.
No source archives, snapshots, Git file enumeration or mode reconstruction
are needed for ordinary local Ruff.

Verification preserves source bytes and the Git index. Ordinary uv/tool caches
and the ignored Ruff cache are allowed. The CLI does not forward arbitrary Ruff
arguments or support automatic fixes or formatting; it explicitly disables
fixing even if repository configuration or the environment requests it.

Native Windows success is **platform-limited local feedback**, not proof that
Unix executable permissions passed. Ruff's
[EXE001](https://docs.astral.sh/ruff/rules/shebang-not-executable/) and
[EXE002](https://docs.astral.sh/ruff/rules/shebang-missing-executable-file/)
are not enforced on Windows. Linux CI/container lint remains canonical and is
required before merge, including genuine executable-file violations:

```sh
uv run --no-project --python 3.13 python tools/delivery.py docker --target test -- ruff check custom_components tests tools
```

Run canonical lint from a Linux checkout. Docker Desktop Windows bind mounts
can expose Git `100644` files as mode `0777`; do not suppress executable-file
rules, add shebangs or rewrite permissions to compensate for synthetic modes.
The native-Ruff exception does not authorize native HA pytest, POSIX checks or
real Core. Those checks remain Docker-based; missing required Docker stops
them, while native Ruff remains available. WSL is not an alternative.

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

CI's [single-candidate matrix handoff](release-workflow.md#ci-candidate-and-matrix-handoff)
transfers one exact package to both Core targets and binds their receipts back
to committed source and Actions backend identities. Local check-cache receipts
are not a substitute for that hosted release evidence.

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

## Environment construction and cache trust

`delivery.py docker-context --target test --output build/test-context` creates a
new minimal build context; `--target core-current` selects the other environment.
The `node` stage supplies checksum-pinned glibc Node.js for minimum `test`, which
adds frozen uv dependencies. The musl-based `core-current` environment instead
copies only Node and its license from a version/digest-pinned official Alpine
Node image, never from minimum `test`. The current-Core build executes Node and
checks its version, so an incompatible binary fails construction rather than
remaining hidden until a later command. It adds no glibc compatibility layer,
package-manager installation, npm or Yarn. Both environments retain Node 22.23.3.

Only the non-installed root project's version is normalized in temporary
`pyproject.toml`/`uv.lock` copies. The real files remain unchanged. A release-only
version change therefore does not rebuild dependencies; changed dependency pins,
shared bootstrap inputs or the relevant recipe still change the environment key.
Current Core does not depend on the minimum dependency lock. Fingerprints identify
construction inputs, not a promise of byte-identical independently rebuilt images.

The [shared build action](../.github/actions/build-image/action.yml) pins Buildx
and BuildKit and uses three bounded GitHub cache scopes: `test`, `core-current`
and `hassfest`. Only a canonical **CI** push on `master` exports cache entries.
PRs, manual Release and HACS acceptance import caches but cannot promote their
builds into the trusted master cache. No image is pushed and no successful test
result is cached. Validation and isolated scenarios still execute.

Measure construction using the native Actions build step. Local `docker` output
records preparation/build seconds and whether an image was already present;
`built: false` after the shared action loaded an image is not proof of a remote
cache hit. Core receipts separately record scenario elapsed time, excluding
host-side image preparation. Compare cold/warm construction separately from
runtime; existing daemon layers make a new image tag only partially cold.

## Real isolated Core fixture (Docker)

`tools/ha_smoke.py` boots actual Core base functionality/registries, required API
components and the integration, using a synthetic identity and a TCP peer at
`127.0.0.1`. The default peer is query-only. It does not mock the production
coordinator, entities, decoder or transport. Each run uses a fresh temporary
config and unloads/stops afterward.

Use `tools/delivery.py docker` to run against the immutable container images
(`--target core-current` for Home Assistant 2026.10.4 on Python 3.14; `--target test`
for minimum supported Core 2026.2.3 on Python 3.13). Images are reused by their
stage-specific input fingerprints without checking mutable `.venv` state.
If Docker is unavailable, stop the workflow and notify the user.

The native local Ruff exception and canonical Linux permission evidence are
defined [above](#native-local-ruff-windows-and-linux); they do not change this
Docker runtime requirement.

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

**Native exchange-trace acceptance.**

The `exchange-trace` scenario extends this same isolated boot with native
`logger/integration_log_level` WebSocket commands (`persistence: none`) and
authenticated HTTP **Download diagnostics** attachments. Run it on both supported
acceptance versions; the receipt records the actual Core/Python versions and
installed bundle hashes rather than inferring acceptance from the image name.

```sh
uv run --no-project --python 3.13 python tools/delivery.py docker --target test -- python tools/ha_smoke.py --bundle build/delivery-candidate --output build/exchange-trace-min.json --scenario exchange-trace
uv run --no-project --python 3.13 python tools/delivery.py docker --target core-current -- python3 tools/ha_smoke.py --bundle build/delivery-candidate --output build/exchange-trace-current.json --scenario exchange-trace
```

The initial parity peer is query-only. It splits an unmatched state frame across
writes, then coalesces the remainder with the correlated reply. Acceptance compares
downloaded TX/RX bytes with the actual peer buffers, verifies the observed read
boundaries and unmatched/correlated chronology, and retains authoritative standby
state. Matched capture-OFF/ON queries must have identical connection/query counts
and zero controls or retries. It also checks unrelated logger notifications,
frozen repeated downloads, no appliance network activity during downloads,
capture-OFF activity without retained events, and OFF/ON reset with no intervening
appliance event.

The same scenario selects explicit legacy 300-operation/6000-event preferences
and executes 360 accelerated ordinary polls: a native download after 240 retains
all 240, and after 360 it retains exactly the newest 300
with the oldest 60 reported as omitted. The receipt records events/raw bytes,
export size and nominal 15-second poll-slot coverage, not an elapsed-hour claim.
Autonomous polling is disabled on the disposable fixture entries from creation;
each explicit refresh still uses the real coordinator/TCP path.

Two entries share native DEBUG but retain independent stores, selected limits and
finite deadlines. Setting up/reloading under existing DEBUG does not start capture:
the fixture explicitly starts the second entry and verifies no automatic resumption.
Unload clears/closes the old store without changing ordinary cached-state behavior.
Logger changes cannot revive an unloaded store.
The fixture revokes its disposable authentication, closes its WebSocket/session,
unloads its second entry and closes its peer; the existing runner unloads the
primary entry, stops Core and removes the temporary configuration. Scenario errors
remain primary if cleanup also fails. Receipts contain only synthetic identities
and exchanges, never temporary token values; an unrelated synthetic credential
sentinel must remain absent from diagnostic downloads.

Authenticated native options HTTP calls exercise input → confirmation → final
Save, selected limits/deadline, preference-only preservation, native global stop,
the native DEBUG menu state, and explicit start across a control-change reload.
Capture settings/start/stop and downloads add no appliance traffic; an independently
required reload still performs its ordinary setup query.

The scenario retains the previous-limit 720-poll/600-operation measurement for
comparison. It also executes representative long-session cases: 960 real ordinary
polls with four-hour 1200/12000/4 MiB settings, and 1440 with six-hour
1800/18000/4 MiB defaults. Each includes one explicitly armed exact synthetic
Spin/400 Start through the registered native action and occasional fragmented,
additional reports. Initial Start proof and every poll must remain complete.
These are accelerated capacity observations, not elapsed hours or physical
appliance actions; isolated peers reject unarmed or unsupported commands.
A separate six-hour case keeps 1800/18000 but selects 385000 raw bytes: native
review with polling enabled estimates 21705 seconds without a capacity warning,
yet actual traffic must evict the initial Start under raw pressure. It records loss and
the retained observation range. Autonomous scheduling is suppressed for this
accelerated fixture after selecting the 15-second reference; no wall-clock
six-hour claim or absence-of-warning completeness claim follows.

Separate direct store-policy stress fills both previous and new simultaneous
operation/event/raw maxima with metadata-heavy observations, and truncates a
9 MiB observation to the 8 MiB raw maximum with explicit loss. These records are
not wire frames. Receipts distinguish raw payload, Python raw objects,
metadata/container graph, compact JSON and additional export/JSON peak. New-limit
acceptance requires those measurements; old measurements do not establish new
costs. Native browser inspection remains separate from this HTTP/WebSocket fixture.

The scenario is public software/transport evidence, not physical appliance or
browser acceptance. Perform independent source review first and run the exact
committed bundle for final evidence. These commands describe the required
acceptance; only a successful recorded execution proves it.

The versioned public Core contracts are the native logger commands
([2026.2.3](https://github.com/home-assistant/core/blob/2026.2.3/homeassistant/components/logger/websocket_api.py),
[2026.10.4](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/logger/websocket_api.py))
and authenticated `/api/diagnostics/config_entry/{entry_id}` download
([2026.2.3](https://github.com/home-assistant/core/blob/2026.2.3/homeassistant/components/diagnostics/__init__.py),
[2026.10.4](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/diagnostics/__init__.py)).
The fixture does not read private Core logger storage or invoke diagnostic
callbacks in place of these native requests.

**Revised retention sizing: measured exact-package #81 continuation.**

The committed integration `275d515` was exercised in isolated Core 2026.2.3,
CPython 3.13.15. Counts below include 25/38 additional-report polls in the
four-/six-hour workloads. The original confirmed Start remains in the first two
cases. Their durations are selected deadlines; execution is accelerated, not
four/six elapsed hours.

| Selected budgets and duration | Executed polls + Start | Retained operations | Retained events | Raw bytes | Omitted operations |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1200 / 12000 / 4 MiB, 4 hours | 960 + 1 | 961 | 9734 | 260703 | 0 |
| 1800 / 18000 / 4 MiB, 6 hours | 1440 + 1 | 1441 | 14586 | 390632 | 0 |
| 1800 / 18000 / 385000 bytes, 6 hours | 1440 + 1 | 1422 | 14372 | 384826 | 19 |

The last case's native enabled-poll review estimates 21705 seconds without a
capacity warning. Actual additional reports nevertheless evict Start: all 19
whole-operation evictions are raw-triggered, with 5806 omitted raw bytes and no
retained partial operation. This demonstrates why no warning is not a completeness
claim. Matching counts were observed under Core 2026.10.4 / CPython 3.14.9.

Memory is a deduplicated reachable `sys.getsizeof` graph, not RSS or exclusive
ownership. The additional `tracemalloc` peak starts after the retained store
already exists and covers export plus compact UTF-8 JSON. Values below use MiB
(1048576 bytes). Both maximum-pressure cases fill 8 MiB raw and all simultaneous
operation/event slots with bounded metadata-heavy observations, not wire frames.

| Workload | Python raw objects | Python metadata/containers | Total store graph | Compact JSON | Additional export/JSON peak |
| --- | ---: | ---: | ---: | ---: | ---: |
| Four-hour representative case | 0.31 | 9.93 | 10.24 | 4.64 | 19.12 |
| Six-hour representative case | 0.46 | 14.88 | 15.35 | 6.95 | 28.66 |
| Previous maxima: 600 / 12000 / 8 MiB | 8.02 | 102.79 | 110.81 | 106.12 | 235.18 |
| New maxima: 1800 / 18000 / 8 MiB | 8.06 | 147.43 | 155.49 | 144.52 | 316.28 |

An 8 MiB raw limit is therefore **not** an 8 MiB memory allowance; diagnostic
export can allocate substantially more memory temporarily. These are measured
fixtures, not a formal process-memory ceiling or a guarantee for arbitrary traffic.
The separate oversized case retains 8 MiB of a 9 MiB observation, marks its
operation partial and reports the omitted 1 MiB.

**Default retention sizing: measured coordinator/TCP sample from #80.**

New/default budgets are 1800 operations, 18000 events and 4,194,304 raw bytes,
with a separate six-hour finite deadline; explicitly saved values remain.
They are configurable within the [documented bounds](../README.md#diagnostic-capture-preferences).
The original 300/6000 sample below is historical reference, not current defaults
or evidence of new maximum RAM costs. All three budgets apply together, including
to one oversized operation. Raw-byte capacity alone never defines total memory.

The following run used Linux Docker, CPython 3.13.15 and actual coordinator
refresh/Start paths with the existing synthetic TCP fixtures. A normal poll
received one correlated report; the fragmented case split an unmatched frame then
coalesced its tail with a correlated report. Start used initialization, an accepted
response and an independent active-state report. Refusal sent no bytes.
Sizes include a fresh store/export envelope; timing/ID lengths can vary.

| Single operation | Events | Raw bytes | JSON metadata/framing bytes | Full compact JSON bytes | Python metadata/container bytes | Python raw-object bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Ordinary poll | 10 | 266 | 4799 | 5331 | 18345 | 332 |
| Fragmented/unmatched poll | 14 | 439 | 6907 | 7785 | 23171 | 538 |
| Confirmed Spin/400 Start | 31 | 845 | 9905 | 11595 | 33654 | 1076 |
| Local Start refusal | 0 | 0 | 2084 | 2084 | 9112 | 0 |

| Executed workload | Retained polls / controls | Events | Raw KiB | Compact JSON MiB | Python store graph MiB | Additional export/JSON peak MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 240 ordinary polls | 240 / 0 | 2400 | 62.34 | 1.14 | 2.52 | 4.71 |
| 360 ordinary polls | 300 / 0 | 3000 | 77.93 | 1.43 | 3.16 | 5.89 |
| 240 fragmented/unmatched polls | 240 / 0 | 3360 | 102.89 | 1.70 | 3.64 | 6.87 |
| 240 ordinary polls + 16 Starts | 240 / 16 | 2896 | 75.55 | 1.31 | 2.91 | 5.42 |

The 360-poll run retained operation IDs 61–360 without partial operations; the
mixed run retained all 256 operations. With the old 20-operation budget, the same
240-poll workload retained only IDs 221–240. These are accelerated capacity
measurements: 240 ordinary 15-second poll slots correspond to about an hour;
300 correspond to about 75 minutes. This does not establish wall-clock coverage
under arbitrary command/report intensity, physical-cycle completeness or latency.

Memory method: walk the captured store's reachable Python object graph using
`sys.getsizeof`, deduplicating object identities, including dictionaries, slot
records, containers and referenced strings once. Raw-object sizes include `bytes`
overhead; remaining graph bytes are metadata/containers, not raw payload. This
is not process RSS or exclusive ownership accounting. Compact UTF-8 JSON uses
`separators=(",", ":")`; subtracting its ASCII hex content gives metadata/framing
size. `tracemalloc` starts after the captured store exists and measures additional
allocations through `export()` and JSON encoding, including transient serializer
buffers. Its peak is additional to the existing store, not a 4 MiB total-memory
promise, and varies with Python/platform and workload. No measurement machinery
runs in the integration or changes production polling.

**Configurable maximum-budget sizing: issue #81 acceptance.**

The same public `exchange-trace` scenario was exercised from the committed package
on minimum Core 2026.2.3 / Python 3.13.15 and current Core 2026.10.4 / Python 3.14.9.
Both retained the same operation/event/raw counts below. Memory/export figures
use the current-Core run and the method above; timestamps and Python versions
can change serialized sizes and peak allocations slightly.

| Workload | Retained operations / events | Original raw bytes | Missing or partial evidence |
| --- | ---: | ---: | --- |
| 240 ordinary TCP polls | 240 / 2400 | 63840 | None |
| 720 ordinary TCP polls | 600 / 6000 | 159600 | 120 oldest operations evicted |
| Direct synthetic combined maxima | 600 / 12000 | 8388608 | None |
| One direct synthetic 9 MiB observation | 1 / 1 | 8388608 | 1048576 raw bytes omitted; operation partial |

| Workload | Python raw objects MiB | Python metadata/container MiB | Python store graph MiB | Compact JSON MiB | Additional export/JSON peak MiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| 240 ordinary TCP polls | 0.08 | 2.45 | 2.53 | 1.14 | 4.71 |
| 720 ordinary TCP polls | 0.19 | 6.12 | 6.31 | 2.85 | 11.78 |
| Direct synthetic combined maxima | 8.02 | 102.79 | 110.81 | 106.12 | 235.18 |
| One direct synthetic 9 MiB observation | 8.00 | <0.01 | 8.00 | 16.00 | 52.01 |

The direct store-policy pressure uses deliberately metadata-heavy observations,
not ordinary appliance frames. It demonstrates why an **8 MiB raw budget is not
an 8 MiB RAM budget**: metadata and transient export allocations are separate.
The additional peak is not total process RSS or an exclusive-memory upper bound.
The oversized case retains 4612 metadata/container bytes, not zero.

The maximum-budget ordinary run keeps the newest 600 complete polls after 720
operations, with honest oldest-first omissions. At 15 seconds per poll that is
9000 seconds of nominal capacity, while the longest capture timer is 7200 seconds.
These accelerated tests establish capacity, not elapsed-hour observation or
guaranteed history under arbitrary traffic. Default-budget acceptance separately
keeps all 240 first polls, then the newest 300 of 360 polls.

The `offline` scenario refuses the first real TCP connection while loading a
previously configured entry. It requires a loaded entry, absent authoritative
snapshot, unavailable readings/Start, then restores the same peer and observes
online → offline → online through ordinary 15-second polling, without forced
refresh or reload. It verifies unchanged entity identities and the normal
artifact, socket-guard and cleanup boundaries.

```sh
uv run --no-project --python 3.13 python tools/delivery.py docker --target core-current -- python3 tools/ha_smoke.py --bundle build/delivery-candidate --output build/offline-smoke.json --scenario offline
```

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

### Programme-intent preparation

The optional `programme-intents` scenario exercises the registered atomic
`start_program` action in current isolated Core: lower Spin/400, upper A1/W30
Smart recipe, lower Mix/Blood/600 and rejected Smart override with explicit
zero delay. Its peer separately arms each independently specified initialization
and grouped Start, returns a correlated confirmation, and rejects different or
repeated commands. The default peer stays query-only; the profiles scenario
retains its separate single Spin/400 allowance.

The receipt records command payloads, confirmed outcomes, preserved interactive
drafts/saved-profile selection and operation-attributed traffic. Scheduled polling
is suspended during measurement; labelled synthetic-readiness refreshes are
separate from action traffic. Invalid preparation must leave authoritative state
and the latest command outcome unchanged and cause no operation-triggered traffic.
All cases and normal unload/Core/peer/temporary-config cleanup must complete.

```sh
uv run --no-project --python 3.13 python tools/delivery.py docker --target core-current -- python tools/ha_smoke.py --bundle build/delivery-candidate --output build/programme-intents.json --scenario programme-intents
```

The protocol consumer regressions also invoke the actual CLI against a bounded
loopback peer, checking ordinary/Smart command content, confirmation, rejected
inputs, parser/handled exit statuses and opt-in/readiness precedence:

```sh
python tools/delivery.py docker --target test -- pytest -q tests/test_protocol.py -k cli_start
```

Both commands use Docker without external networking and synthetic identities,
not a real appliance. Offline CLI `decode` is not recipe-preparation proof.
Keep detailed presence/null/empty cases in focused regressions rather than
repeating a full recipe/Core/adapter matrix. Required minimum/current acceptance
is unchanged; add a minimum-Core action smoke only for a concrete changed
compatibility dependency. No browser or physical-appliance acceptance is implied.

The `discovery` scenario exercises real UDP collection and native config-flow
steps against loopback peers: enabled-adapter prefix calculation, continuous
20-second queries beyond the former 120-second limit in default **full** mode,
repeated ephemeral/fixed-port replies, exact counters and explicit stop without TCP enrichment,
unsupported/unknown/configured inspection, supported addition and explicitly
confirmed manual fallback with actual TCP validation. Collection starts only
after preview ownership, never merely from preparing a form.
The disposable HA network inventory is synthetic; this does not prove physical
LAN broadcast egress. `--serve` keeps the peers available for actual stock form/
preview, animation continuity, Close and WebSocket cleanup acceptance.

The same scenario also exercises the three discovery lifecycle findings from
[#62](https://github.com/Zuz666/uplus-reborn/issues/62), without a second runner:
it gates preview translations, waits for Core's real authenticated WebSocket
disconnect cleanup, releases initialization, and checks that no search owner or
UDP query survives while an unrelated interval crosses its next round.
Full mode uses the normal 20-second cadence. Two connected native-preview
consumers then receive Stop's terminal
payload without its searching animation; repeated cleanup and later real UDP
replies must leave the old observations frozen and a new interval unaffected.
Finally, an interval remains active through the runner's existing, actual
`hass.async_stop()` teardown. Only afterwards does the fixture check the shutdown
outcome, settled collection, logged errors and exclusive rebinding of both
discovery UDP ports, then dispose its own peers and synthetic network inventory.

`discovery_lifecycle` in the receipt records a separate status and observations
for `preview_disconnect`, `stop_publication` and `shutdown`, plus disposable-auth,
WebSocket, fault-hook, socket and fixture cleanup evidence. Failed findings are
retained and make the run fail after teardown; no finding is reported as fixed
merely because initialization returned. The receipt includes the actual Core
version but no temporary token values. These socket/payload checks do not prove
browser rendering: use current-Core `--serve` for the separate native frontend
acceptance. Run the same scenario with `--target test` for minimum-Core evidence.

The same runner's `discovery_shared_sender` receipt separately exercises
[#63](https://github.com/Zuz666/uplus-reborn/issues/63). Two enabled synthetic
loopback interfaces use distinct prefix-derived destinations and real UDP
sockets. Staggered subscriptions must not send immediately, import historical
records/counters or reset the shared timer. Three, then two, then one active
interval observe one query per usable sender/destination pair per round, with
actual packet timestamps spanning unchanged 20-second sleeps in full mode. Independent Stop
freezes each interval's own result; exclusive rebinding proves last-owner socket
release before a fresh immediate run. The receipt retains per-round packet
counts, source/destination/port/timestamps, per-interval counters and outcomes.

Controlled seams wrap only the collector's send transports: an all-pairs
`OSError` round must remain nonterminal, restored transports must send again on
the next round, and an unexpected sender exception must fail every active
interval while preserving its own partial records and coverage diagnostics.
A stopped interval remains unchanged. A reserved loopback fixed port and a
production UDP protocol error callback cover recoverable open/receive warnings;
these are synthetic faults, not proof of physical network-error behavior.
A new explicit search must clear the failed run's issues. During the actual
final Core stop event, a new subscription attempt must admit no sender.
Fault hooks and the temporary two-interface inventory are restored in `finally`.
Only `discovery` extends the bounded boot/scenario budget from 180 to 300 seconds
to accommodate six additional real 20-second waits; ordinary scenarios and
native `--serve` behavior retain their existing limits. Run receipts are new
evidence only when actually executed; this description does not re-prove or
replace the earlier, separate issue #62 evidence.

```sh
uv run --no-project --python 3.13 python tools/delivery.py docker --target core-current -- python3 tools/ha_smoke.py --bundle build/delivery-candidate --output build/discovery-smoke.json --scenario discovery
```

**Discovery timing modes and CI selection.**

Existing commands remain full by default. For local functional feedback without
the real 122-second dwell and 20-second inter-round waits, explicitly select fast:

```sh
uv run --no-project --python 3.13 python tools/delivery.py docker --target test -- python3 tools/ha_smoke.py --bundle build/delivery-candidate --output build/discovery-fast.json --scenario discovery --discovery-timing fast
```

Fast mode retains real Core, UDP/TCP, authenticated preview connections, faults
and actual shutdown. A fixture-private adapter holds only discovery's requested
20-second waits and releases the next round after the required observations.
It does not replace global asyncio timing, send artificial queries, fabricate
responses or shorten production cadence. Hooks are restored before interactive
`--serve` and after exceptions/cancellation. Unrelated Core timers are unchanged.

The receipt's `discovery_timing` records mode/reason, measured phase durations
and separate real-time coverage. Fast mode reports actual-cadence/continuous-dwell
proof as `not_applicable`: several controlled rounds are not 122 real seconds
of searching or actual 20-second packet spacing. Raw UDP timestamps remain real.
An unexecuted phase is `not_run`; failures remain failures through teardown,
with the initiating scenario error retained separately from a later cleanup error.

PR CI selects fast only for a complete, revision-bound comparison positively
classified as unrelated. Discovery/callers, shared dependencies, environment,
verification inputs and unknown/incomplete/stale comparisons select full.
Both sides of renames and deletions count; an unrelated final commit cannot hide
an earlier relevant change. Non-PR/master/manual contexts remain full.
The selector supports forcing full, not bypassing applicable full coverage.
The runner validates that a CI selection belongs to its committed bundle.
No job or Core target is skipped, and the release-source matrix is unchanged.

Each Core job appends a receipt-based GitHub Job Summary for the existing five
scenarios, including status, real elapsed duration, discovery mode/reason and
phase timings. Missing evidence is unavailable, not zero or successful execution.
Phase durations are diagnostic, not a second additive total or cached acceptance.
No unmeasured total-CI, image-preparation or billing saving is implied.

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
`standby`, `running`, `paused`, `offline`, `online` or `stop` commands on `stdin`,
and revokes the token and removes the user before unloading the entry and writing
`--output`. `offline`/`online` close/reopen only the synthetic TCP peer; subsequent
production polling, not a forced refresh, observes the changed connectivity. Clean
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

### CI platform coverage

Coverage is fixed by the actual caller event, not changed paths, PR metadata or
a reduced-coverage input. Reusable workflows inherit the caller's GitHub context.
Only `pull_request` selects the smaller policy; master push, manual dispatch
and unknown contexts retain full coverage.

| Check | PR verification | Full verification |
| --- | --- | --- |
| Repository Markdown/Mermaid content (`npm run test:docs`) | Linux | Linux and Windows |
| Documentation runner regressions (`npm run test:docs:tooling`) | Linux and Windows | Linux and Windows |
| Public tooling: locked native Ruff, standalone tests, committed metadata | Linux and Windows | Linux and Windows |
| PR-label classification and metadata safety (`npm run test:labels`) | Linux | Linux and Windows |

PR CI has 17 actual jobs; full CI retains the existing 18 job names and meaningful
commands. Windows documentation still installs tooling and executes runner
regressions on PRs; its skipped repository-content step is explicitly reported
as not applicable, not successful coverage. The Windows label row is absent
from the PR matrix, not an empty successful job.

Required failures, cancellations and unexpected skipped execution remain
non-successful; the aggregate gate requires all four workflow layers to succeed.
There are no changed-file filters: docs-only, integration, tooling, lock/config,
rename/delete and unknown-path PRs all receive the same platform coverage.
The [release-source policy](release-workflow.md#trusted-master-consumption) still
requires the exact full master job set and source run/attempt. A reduced PR run
or a manual task-branch run cannot authorize release.

This removes repeated PR work, not supported-platform coverage. Job durations
include preparation; omitting one content command does not save an entire
Windows documentation job. No overall CI speedup or billing reduction is implied.

Each minimum/current Core job uploads its exact committed release candidate
alongside the synthetic receipts. A separate same-run artifact job downloads
both candidates into independent directories and verifies their safe bundles,
ZIP members, checksums, notes and provenance against the checked-out commit.
This exercises the real GitHub Artifacts backend without publishing a release,
executing downloaded code or consuming another run's privileged artifacts.

Uploads retain archived format and explicit artifact names. Raw single-file
upload mode changes name/layout semantics and is not used for release
candidates or synthetic receipts.

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
| Real Core | 2026.2.3 and 2026.10.4: standby, execution, profiles including restart, discovery, discovery-identification and native exchange-trace |
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

For actual published downloads, use the maintained
[`hacs_acceptance.py` command](release-workflow.md#repeatable-published-hacs-acceptance).
It verifies immutable tag/source/asset identity, then uses real HACS and the same
fresh-process user-data invariants. The
[read-only workflow](../.github/workflows/hacs-acceptance.yml) can repeat an
existing release's acceptance independently of publication, including historical
tags that are no longer latest. Its ephemeral GitHub read token is available to
installer workers only, never Core workers. A failure is explicit and does not
authorize republishing or changing immutable assets.

## Completion, not endless rechecking

A change is ready when its declared behavior is exercised, affected contracts are
consistent, evidence identifies the exact inputs/environment, and known limits
are explicit. Runtime/deployment work also binds the actual package bytes.
Reopen a check only for changed relevant inputs, a different scenario/environment
or contradictory evidence—not because another stage starts. Authorization and
fresh physical preconditions are never cached as software evidence.

Licensed under [CC BY 4.0](LICENSE). Attribution: U+ Reborn contributors,
[U+ Reborn repository](https://github.com/Zuz666/uplus-reborn).
