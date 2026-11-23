# Operational and technical reference

Detailed behavior, safety boundaries, and configuration notes for **U+ Reborn**
on the **Haier HWD120-B1558U**. For day-to-day setup and automation examples,
see [README](../README.md); for bilingual entity names, see the
[entity-name register](terminology/hwd120-b1558u-entity-naming.md); for wire
framing and byte layouts, see the
[HWD120-B1558U protocol specification](protocol/hwd120-b1558u.md).

## Local network discovery

### Search cadence, ports, and live preview

**Find on local network** discovers compatible devices over UDP without a Haier
account or internet access:

1. Open **Settings → Devices & services → Add integration → U+ Reborn** and select
   **🔎︎ Find on local network**.
2. The first active search sends immediately once its listeners are ready, then
   active searches share queries every 20 seconds per usable enabled IPv4
   interface. A later search waits for the next shared round without sending an
   extra query or resetting the schedule.
3. Choose a **Discovered device** from the catalog dropdown, select
   **View device / last activity**, and press **Confirm** to inspect its card or
   **Technical details**. For a new supported HWD120-B1558U, choose
   **Add this supported device** and confirm. Addition requires a fresh
   checksum-valid, read-only TCP state check on port `56800`.

Firewall and network rules:

- Discovery sends UDP broadcasts to destination port **`7083`** using actual
  configured adapter prefixes, not a guessed `/24`.
- Replies can return to either the sending socket's ephemeral port or UDP/`7083`.
  Allow return traffic to both; do not filter replies solely by port `7083`.
- Socket or interface open failures are reported as partial coverage. Silence
  does not prove absence: VLAN separation, client isolation, blocked broadcasts,
  or disabled Home Assistant network adapters can prevent UDP replies. Discovery
  never reconfigures your network; use **✎︎ Connect manually** when broadcast
  discovery cannot reach the appliance.

Preview and catalog controls:

- **Sent / Replies / Found / Msg** reports shared UDP submissions observed by
  this search, valid replies received, distinct devices found, and the latest
  event. **Sent = 0** while waiting for the next round is normal, delayed
  replies can arrive before the first round, and simultaneous dialogs share
  submissions (do not add counters across dialogs). **Device replied** means an
  already-found device replied again.
- The animated washer ↔ Home Assistant illustration is decorative, not
  delivery or acknowledgement proof; reduced-motion settings display a static
  image.
- **Show found devices** stops only that dialog's search and opens the catalog
  without adding a device or probing TCP. There is no fixed timeout or auto-stop
  duration. Closing the dialog or losing the preview connection stops only its
  search.
- A fatal shared-sender failure stops active searches with diagnostics while
  keeping each search's valid observations for inspection; restart explicitly.
- **Last search activity** at the end of the catalog dropdown opens a frozen
  journal of up to 12 recent events, cumulative counters, and coverage warnings
  without permitting Add or a TCP probe.
- The catalog has three radio actions (**View device / last activity**,
  **Continue searching**, and **Enter manually**) plus **Confirm**. Searching
  and manual entry work even when the catalog is empty.

### Offline device catalog and duplicate boundaries

The bundled offline lookup combines inspected APK category facts with 11 model
names across 7 exact Type IDs from the manufacturer's product-selection
catalog (retrieved 2026-10-20):

- Matches are catalog/profile lookups, never model names read verbatim from UDP,
  and do not prove global Type ID uniqueness.
- Shared Type IDs list **all** possible candidate models on the device card
  (for example, `H3F-280WSAAU1`, `H3F-320FSAAU1`, and `H3F-320WSAAU1`).
  Category-only and unknown types stay visible without guessed model names.
- Only **HWD120-B1558U** has project hardware correlation. Unsupported and
  already-configured devices receive no extra TCP or cloud probes and cannot be
  added from their cards.
- Disabled or unloaded U+ Reborn entries still block duplicate creation;
  another integration's exact identity association is shown as informational
  context, not a hard block.
- Browsing the discovery catalog is strictly onboarding-only: it never updates
  existing entry addresses, reloads entries, changes preferences, or clears
  programme drafts. There is no mDNS discovery, runtime UDP address recovery,
  Wi-Fi pairing, or automatic cloud fallback.

### Cloud, DHCP, manual, and reconfigure behavior

- **☁︎ Use Haier account:** Connects over HTTPS to `uws-euro.haieriot.net`
  once during onboarding for an appliance already paired in the Haier U+ app,
  fetches its saved name and Device ID, resolves its LAN IP via local discovery
  (with a manual host input fallback), and validates a checksum-valid TCP state
  response before saving. Email, password, tokens, and user IDs are discarded
  before creating the entry.
- **Automatic DHCP discovery:** Matches the verified `98AAFC*` Wi-Fi OUI in the
  integration manifest, derives the 12-character uppercase Device ID from the
  DHCP MAC, uses a non-empty DHCP hostname (or `HWD120-B1558U`) as the initial
  entry title, and confirms the model after a live read-only TCP check. Later
  DHCP events update the entry's IP address automatically without changing
  entity IDs or history.
- **✎︎ Connect manually:** Enter the IP/hostname, port `56800`, and the
  appliance Wi-Fi MAC converted to **12 uppercase hexadecimal characters without
  separators** (for example, `02:11:22:33:44:55` becomes `021122334455`), then
  explicitly confirm **HWD120-B1558U**. Without a UDP reply, TCP alone proves
  protocol reachability, not the commercial model. Known incompatible type,
  security, or conflicting identity evidence observed in the current dialog
  cannot be overridden manually.
- **Reconfigure:** Available on existing entries to update host/port with a
  read-only state check (never starts or cancels a cycle). Retaining identity,
  entity IDs, customizations, and history, a changed address reloads a loaded
  entry once or schedules setup for an unloaded/failing entry (without
  re-enabling an explicitly disabled entry). An unchanged address on a loaded
  entry skips reloading.

## Control execution and safety

### Transaction lifecycle and postcondition confirmation

Every remote control command (`Start`, `Pause`, `Resume`, `Cancel`,
`Standby`, `Sound`, `Child lock`, `Appliance power`, `uplus_reborn.start_program`,
and `uplus_reborn.start_profile`) executes under the following pipeline:

1. Acquires the per-appliance lock shared with polling and other commands, and
   re-checks the live remote-control opt-in.
2. Reads fresh appliance state (`4d01`) and alarm state (`0x73 -> 0x74`),
   verifying action-relevant state revisions and safety preconditions. Countdown
   minute ticks alone do not invalidate an otherwise unchanged ready state.
3. Constructs one read-modify-write frame (preserving shared and healthy-drum
   flags).
4. Dispatches the command **once** and requires correlated wire response or
   fresh postcondition proof, followed by one best-effort post-confirm
   `0x73 -> 0x74` alarm refresh (which never downgrades a confirmed write into an
   unknown outcome).
5. Reports **outcome unknown** if confirmation is lost and never retries the
   write automatically. Unconfirmed entities become unavailable until ordinary
   polling recovers; polling recovery never retroactively confirms an uncertain
   write.

Key protocol safety boundaries:

- **Raw `05` is not an ACK:** If an appliance replies with empty type `05` or a
  delayed transition, independent fresh-sequence proof reads may confirm the
  postcondition within the original deadline without resending the command.
- **Confirmed active faults (`0x04` / `0x74`):** Block all five lifecycle
  actions (`Start`, `Pause`, `Resume`, `Cancel`, `Standby`) on the faulted drum
  while leaving the healthy other drum and shared switches (`Sound`, `Child lock`,
  `Appliance power`) operable.
- **Unconfirmed alarm state (`unknown`):** Fails closed on `Start` and `Resume`
  while keeping `Pause`, `Cancel`, and `Standby` available in the UI (and
  re-checking `0x73 -> 0x74` before dispatching any write).
- **Cancel vs. drainage / door position:** `Cancel` confirms only a return to
  phase-zero standby (`cycle_active = false`), **never** water drainage or safe
  physical door opening. `Door lock` reports only the raw appliance lock flag,
  never physical door ajar/closed position.
- **Standby (completed-state reset):** Sends the appliance reset/cancel command
  (`4d13` / `4d93`) from completed state rather than resetting Home Assistant
  state locally.
- **Pause/Resume gating:** Uses the source cycle-active flag rather than motor
  motion claims. `Pause` requires `Running`; `Resume` requires `Paused`. In
  `button.press` automations, target specific button entities rather than a
  whole device or area (mixed targets can partially succeed without rollback).
  Per Home Assistant
  [button state rules](https://www.home-assistant.io/integrations/button/#button-states),
  button state records the last HA press attempt timestamp (including rejected
  presses), not appliance execution state.
- **Program phase vs. physical stage:** Phase labels use the exact profile
  vocabulary with hardware-observed corrections (`0x0e` / `0x10` map to
  `Intermittent rotation`, not completion). Timed `Dry` can briefly report
  `Washing` (`0x05`) before `Drying` (`0x0c`), with reported `Wash-stage time` at
  zero rather than echoing the drying duration.
- **Unconfirmed settings and source quirks:** Diagnostics distinguish confirmed
  writes from settings the appliance state frame does not echo: selected
  `Spin (RPM)`, numeric `End delay (h)`, and `Refresh` drying duration. On
  `Refresh`, the exact-model builder preserves the reported drying byte instead
  of inventing a wire value for its sole 30-minute UI choice.

### Shared switches and integration options

- **`Sound`, `Child lock`, and `Appliance power`** apply appliance-wide and
  dispatch immediately on toggle (no **Start** press needed). Their displayed
  state reflects authoritative appliance reports rather than optimistic local
  state, and requires both control opt-in and command preconditions.
- **`Sound`:** Controls the appliance sound setting (not HA notifications). Which
  specific buzzer tones (buttons, end-of-cycle, alarms) are silenced has not been
  independently verified on hardware.
- **`Child lock`:** Tracks the appliance child-lock setting flag, not physical
  panel lockout timing (the appliance manual notes the lock icon appears one
  minute after Start). It does not control drum door locks. When `Child lock`
  and `Local control lock` turn on together, turning `Child lock` off remains
  allowed while powered and in Remote Control mode.
- **`Appliance power`:** Disabled by default in the Home Assistant entity
  registry. Power-off removes physical Remote Control, so this switch cannot
  replace **Cancel** and cannot reliably wake an offline appliance.
- **Integration settings (`Configure → Integration settings`):**
  - *Appliance connection (when opened):* Snapshot of health at dialog open
    (`Connected`, `Offline`, or `Not yet known`); opening never polls or updates
    live while open.
  - *Polling interval (seconds):* Whole number `15–30`, default `15`. Updating
    only the polling interval takes effect on future polls without reloading the
    entry or clearing drafts.
  - *Enable remote controls:* Off by default. Toggling opt-in reloads a loaded
    entry and clears both drafts and profile-name text fields, while preserving
    entity IDs, custom names, icons, areas, hidden flags, and disabled entities.
  - *Read-only fixed programme settings:* Where the exact profile allows only one
    value (for example, `Tub Clean` at 90 °C and 600 RPM, offering only
    `Night wash`), those selectors stay unavailable for editing while their
    fixed values still participate in Start encoding.

### Saved programme profiles and automation contracts

- **Storage and lifecycle:** Saved profiles persist per config entry in Home
  Assistant `.storage` across reloads, restarts, and integration upgrades (and
  are removed if the integration entry is deleted — include Home Assistant
  backups). Each saved record captures the complete next-run draft: programme or
  Smart fabric, all applicable scalar values, enabled **and disabled** options,
  stains, lower drying, and end delay — never reported settings, live counters,
  or shared `Sound`/`Child lock`/`Appliance power` states.
- **Native UI rules:** `Upper/Lower New profile` text inputs (1–100 characters)
  are transient and clear alongside drafts on reload, restart, or control opt-in
  change. Profile names must be unique per drum (case-insensitive); `None`,
  `Unknown`, and `Unavailable` are reserved, and duplicate names fail cleanly
  rather than overwriting. Restoring via `Upper/Lower Saved profile` replaces the
  drum's draft atomically without sending network traffic; manually editing the
  draft afterward switches the selector display to `None (independent draft)`
  without changing the saved profile.
- **Incompatible stored revisions:** Upgrades never synthesize missing fields
  from defaults. If a stored revision becomes incompatible, applying or starting
  it fails clearly while leaving the record available to rename, overwrite via
  `update_profile`, or delete.
- **Action keys and `device_id` rules:**
  - `device_id` is always the Home Assistant device-registry ID (never the
    UWT device ID or MAC) and must resolve to one loaded, opted-in entry.
  - `apply_profile` / `start_profile` accept stable `profile_id` values so
    renaming a profile does not break automations; `start_profile` snapshots one
    immutable revision before queuing on the `start_program` lock path.
  - Smart fabric/stain UI selectors (`Upper/Lower Smart washing` and `Stains`)
    use lowercase translated keys (`f1`, `w15`), whereas `uplus_reborn.start_program`
    expects uppercase source IDs (`F1`, `W1`, `W15`) and rejects any manual scalar
    or option fields (including explicit `delay_hours: 0` or
    `enabled_functions: []`).
  - Ordinary stain removal (`blood`, `grass`, `wine`, `juice`, `soil`, `milk` on
    upper `Kids´Wear`/`Casual` or lower `Cotton`/`Mix`/`Synthetic`) locks
    temperature and wash-stage duration to the recipe (published in the `Stains`
    entity attributes `next_run_temperature_c` and `next_run_wash_stage_minutes`)
    and clears/disables `Eco. wash`, `Night wash`, and `Soak wash`. Combining
    an ordinary stain with manual `temperature_c`, `wash_minutes`, or `eco`/
    `night`/`soak` in `start_program` is rejected.

## Entity categories and readback

When remote controls are enabled, **U+ Reborn** registers **96 entities**:

- **48 read-only status entities (35 Primary + 13 Diagnostic):**
  - *Primary (35):* `Upper/Lower Door lock`, `Upper/Lower Programme now`,
    `Upper/Lower Phase`, `Upper/Lower State`, `Upper/Lower Time left`, plus
    25 reported cycle-setting entities (`Target temp.`, `Wash-stage time`,
    `Rinse count`, `Spin-stage time`, `Lower Drying mode`, and the 16 per-drum
    reported flag binary sensors ending in `on` or `active`).
  - *Diagnostic (13):* `Power`, `Remote control`, `Local control lock`,
    `Power-on water`, `Cumulative water`, `Power-on energy`,
    `Cumulative electricity`, `Upper/Lower Cycle count`, `Upper/Lower fault`, and
    `Upper/Lower fault code`. Diagnostic entities remain enabled by default (for
    health inspection and Recorder history) while excluded from auto-generated
    dashboards and default voice assistant exposure.
- **48 opt-in control entities (19 Select, 15 Switch, 12 Button, 2 Text):**
  - *Controls:* 9 selects × 2 drums + `Lower Drying` (19); 6 option switches × 2
    drums + shared `Appliance power` (disabled by default) (13); `Start`,
    `Pause`, `Resume`, `Cancel`, `Standby`, `Save profile` × 2 drums (12 buttons);
    `Upper/Lower New profile` (2 text).
  - *Configuration (2 Switches):* `Sound`, `Child lock`.

Entity names are created in the Home Assistant server language at registration
time; state, option, and programme labels follow the active user frontend
language while underlying keys remain stable. Exact compact names, including
U+200A hair spaces used for card sorting, are catalogued in the
[entity-name register](terminology/hwd120-b1558u-entity-naming.md).

### Phase vs. execution state and readback limits

- **`Phase` vs. `State`:** `Phase` reflects the current programme step, while
  `State` (`Running`, `Paused`, `Standby mode`, `Completed`, `Delayed`) combines
  phase, cycle-active, and reservation flags from `6d01`:
  - `Completed` or `Drying finished` phases report `Completed` regardless of
    cycle activity.
  - `Standby mode` or `Delayed start` with reservation active reports
    `Delayed`; `Standby mode` without reservation or cycle activity reports
    `Standby mode`.
  - Unsupported phases, or inconsistent combinations (active standby or delay
    without reservation), report `Unknown`.
  - `State` remains derived strictly from `6d01` even if `0x73 -> 0x74` alarm
    state is `unknown` or actively faulted; losing authoritative `6d01` state
    sets status entities to `Unavailable`.
- **Reported cycle settings:** Update whenever any client (physical panel,
  U+ app, or Home Assistant) runs or reserves a cycle, including `Paused` and
  `Intermittent rotation`. Ordinary `Standby mode`, `Completed`, unknown states,
  and inapplicable fields report `Unknown` rather than stale selections.
  Unconfirmed settings (`Spin (RPM)`, numeric `End delay (h)`, original
  main-wash duration, active ordinary stain removal, and exact Smart
  fabric/stain selections) are never fabricated from next-run drafts. A
  retained stain code in the wire report is preserved only in diagnostics.

Existing entity IDs, unique IDs, custom names, icons, areas, disabled/hidden
flags, display preferences, units, and Recorder history are preserved across
integration upgrades.

For users who want one combined dashboard/status entity built from these
authoritative readback sensors, see the ready-to-copy English and Russian YAML
examples in [Summary template sensor](summary-template-sensor.md).

## Consumption accounting

All four consumption entities retain Home Assistant water/energy device
classes, literal appliance-reported units (`L` and `kWh`), and `total_increasing`
state classes using the profile's identity (`#1&1,0`) transform:

- **Important caveats:** These are appliance-reported counters, **not**
  calibrated utility meters or guaranteed whole-machine totals. Neither pair is
  separated per drum or guaranteed to count every programme path identically.
- **Session vs. cumulative behavior:**
  - `Power-on water` and `Power-on energy` accumulate across cycles until a
    panel power-off resets them.
  - Commit timing varies by programme and exit path: some upper `Spin` runs
    committed cumulative updates during intermittent rotation (`0x0e`), lower
    `Bedding`/`Curtain` washes committed cumulative water (`+65`/`+55`) and
    electricity (`+40`) on `0x0b -> 0x10` while updating power-on counters on
    manual exit from `0x10 -> 0x00`, and panel power-off can discard pending
    cumulative updates. Counter persistence across mains loss is unverified.
- **Dashboards:** If these appliance-reporting limitations are acceptable,
  configure **`Cumulative electricity`** in the Energy dashboard and
  **`Cumulative water`** for Water tracking. Never sum or configure both the
  `Power-on` and `Cumulative` pairs together as independent sources.

See [Consumption accounting contract](protocol/hwd120-b1558u.md#consumption-accounting-contract)
for raw word layouts and captured deltas.

## Diagnostic capture preferences

Open **Configure → Integration settings** and expand the **Diagnostics** section
to inspect or update budgets for your **next** capture. Expanding or saving
without checking start leaves any running capture untouched and never reloads the
entry for capture-only edits.

| Resource | Default | Editable range |
| --- | ---: | ---: |
| Operations, including the current one | 1800 | 1–1800 |
| Event records across operations | 30600 | 1–30600 |
| Original raw bytes | 4 MiB (4,194,304 bytes) | 1–8,388,608 bytes (8 MiB) |
| Auto-stop duration | 6 hours | 30 minutes, 1 h, 2 h, 4 h, 6 h |

Notes on configuration and capacity estimates:

- Diagnostic limits accept whole numbers; saved preferences from dialog open
  stay shown as the baseline. **Restore diagnostic defaults** overrides all four
  diagnostic inputs (even invalid edits) on confirmation without changing
  polling intervals, control consent, or an active trace.
- Clicking **Next** displays a review step showing `will change` / `unchanged`
  values, an estimated ordinary-poll trace depth in seconds (assuming 17 events
  and 464 raw bytes per `4d01 -> 6d01` + `0x73 -> 0x74` poll; 0 when Home
  Assistant polling is disabled), and a non-blocking warning if reference
  capacity is shorter than the selected deadline.
- For reference: defaults (`1800 / 30600 / 4 MiB`) cover more than 1,440
  reference polls (~6 h at 15 s intervals); `1200 / 20400 / 4 MiB` covers more
  than 960 (~4 h). Commands, fragmented reads, or frequent unsolicited reports
  shorten capacity.
- Checking **Start a new diagnostic capture and enable DEBUG** shows a
  replacement warning on **Next** and starts only after final confirmation
  (replacing any prior capture). If a simultaneous control opt-in change reloads
  the entry, capture starts on the new coordinator.
- All integration preferences are UI-only; there is no duplicate YAML setup or
  options schema.

## Temporary exchange tracing

Temporary exchange tracing records UWT operations (polls and control attempts),
transmitted/received byte chunks, decoded observations, and coordination
decisions strictly in memory:

- **Start routes:** Either confirm **Start a new diagnostic capture and enable
  DEBUG** in **Integration settings → Diagnostics**, or invoke the integration
  entry's native **Enable debug logging** action (which starts finite captures on
  loaded entries using each entry's saved preferences). Neither opens extra
  connections, alters polling intervals, or retries writes.
- **Ring buffer & memory:** All three budgets apply simultaneously with
  oldest-first operation eviction; once full, recording continues overwriting the
  oldest operations until stopped, flagging partial operations or omissions. Raw
  bytes are hex-encoded on export, so raw-byte budgets are **not** total RAM or
  JSON limits — see
  [diagnostic capture retention sizing](development-verification.md#diagnostic-capture-retention-sizing).
- **Stop & ownership:** Timer expiry stops collection and retains the frozen
  trace for nondestructive offline **Download diagnostics**. Native **Disable
  debug logging** stops all entries at once. Integration reload/unload or Core
  restart clears captured traces without auto-resuming even if DEBUG stays
  enabled. When captures restore DEBUG logging on expiry, automatic restoration
  is skipped if an external logger change happened in the meantime (leaving
  DEBUG on until manually disabled). Onboarding, discovery, and CLI sessions are
  never captured.

> [!WARNING]
> Standard integration configuration and state diagnostics redact host and
> device identifiers. However, when temporary DEBUG exchange tracing has run,
> the downloaded **`exchange_trace` section is NOT anonymized**: it can contain
> local IP addresses, MAC/device identifiers, and complete local UWT byte
> traffic. Review and redact it before sharing publicly. Never publish account
> tokens, APKs, or raw captures.

`health.latest_command` summarizes the latest command result and sanitized cause
(for example, pressing **Start** with no staged programme records
`not_dispatched` / `program_not_staged`, not a transport or appliance fault).

## System Health

Open **Settings → System → Repairs → ⋮ → System information** (or click
**Visit** on the washer's Home Assistant device card; **Manage** opens the
integration page) to view **U+ Reborn** System Health.

The panel reads cached integration state only — opening it sends **zero**
appliance or cloud requests. It displays integration version, entry counts,
configured polling interval, freshness and age of the last successful read, last
sanitized failure category (`connect` or `response` stage, correlation, and
duration, preserved without DEBUG), plus any capture status marker, deadline
timestamps, and retained/free capacities as scalars at snapshot time. Close and
reopen System Information to refresh values.

Licensed under [CC BY 4.0](LICENSE). Attribution: U+ Reborn contributors,
[U+ Reborn repository](https://github.com/Zuz666/uplus-reborn).
