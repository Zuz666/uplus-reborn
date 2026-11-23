# Changelog

A dated entry is not confirmation of publication. Published versions and assets
are listed in
[GitHub Releases](https://github.com/Zuz666/uplus-reborn/releases).
Unpublished development versions are not historical public releases.

## [1.1.0] - 2026-11-23

- **Alarm decoding, fault sensors, and safety interlocks:** Decode appliance `0x04`/`0x74` alarm bitmaps and expose four new Diagnostic entities (`Upper/Lower fault` binary sensors and `Upper/Lower fault code` enum sensors with bounded `active_faults` attributes). Confirmed active faults block lifecycle actions on the faulted drum while leaving the healthy drum and shared switches operable; unconfirmed alarm state fails closed on `Start` and `Resume` (`#92`). Classify unsolicited `0x72` frames and perform a single bounded read-only `0x73` retry on `0x72`/`0x74(seq=0)` collisions (`#94`).
- **Configurable diagnostic exchange tracing:** Add passive, memory-only UWT exchange tracing with configurable per-entry budgets (default `1800` operations, `30600` events, `4 MiB` raw bytes up to `8 MiB`, and 6-hour auto-stop), a two-step **Integration settings → Diagnostics** review with trace-depth estimates and one-shot **Restore diagnostic defaults**, native **Enable/Disable debug logging** lifecycle ownership, System Health capture/failure-stage visibility, and preserved `health.latest_command` cause and stain provenance (`#79`, `#80`, `#84`). Review and redact the unredacted `exchange_trace` section before sharing diagnostic downloads publicly.
- **Local network discovery:** Share UDP discovery sends across simultaneous search dialogs and settle preview-disconnect, explicit stop, and Home Assistant shutdown cleanup (`#64`, `#65`).
- **Documentation:** Publish the model-scoped HWD120-B1558U captured packet protocol reference (`#55`), streamline `README.md` for HACS, extract operational details to `docs/reference.md`, and add English and Russian summary template sensor examples in `docs/summary-template-sensor.md` (`#98`).
- **Compatibility and maintenance:** Minimum Home Assistant Core remains **2026.2.3**; current isolated Core verification baseline updated to **2026.11.2**. Restart Home Assistant after upgrading; existing entity/statistic identities, preferences, units, and saved profiles are preserved. Hardened release draft numeric-ID validation (`#68`), locked native Ruff checks (`#59`), and routine CI/dependency updates. Controls remain opt-in; no additional appliance models or live hardware coverage beyond documented evidence are claimed.

## [1.0.2] - 2026-10-23

- Simplify the English and Russian options dialog, with concise remote-control and polling help plus a read-only appliance connection status captured when the dialog opens. Status follows the frontend language, including when it differs from the Core language, and opening options sends no extra appliance request.
- Load already configured washers when they are switched off or temporarily unreachable. Readings and appliance commands stay unavailable until a fresh valid response; ordinary polling restores communication automatically without recreating the entry or restarting Home Assistant. The offline message explains possible power/network causes and respects disabled polling.
- Keep onboarding validation, protocol/configuration errors, command safety, entity identities, saved profiles and history unchanged. Existing uncertain command outcomes are never replayed automatically.
- Maintenance: release publication now consumes the exact trusted master CI candidate and complete runtime matrix, with repeatable real HACS installation/upgrade acceptance and stage-specific environment caching. No additional hardware coverage or live deployment is implied.

## [1.0.1] - 2026-10-22

### Fixed

- Disable scalar programme selectors when the programme offers no choice.
  Tub Clean no longer allows edits to its fixed settings; Night wash remains
  its only additional function. Fixed temperatures in upper Allergy Care and
  lower Hygiene are also disabled. Adjustable settings become editable again
  when an ordinary programme is selected ([#1](https://github.com/Zuz666/uplus-reborn/issues/1)).
- Continue read-only state queries while a dispatched control awaits confirmation,
  without blocking independently matching appliance reports. This fixes the
  confirmation stall identified in the lower-drum Tub Clean Start logs. The
  original deadline, strict postconditions and single control write remain;
  missing authoritative evidence still produces an unknown outcome
  ([#2](https://github.com/Zuz666/uplus-reborn/issues/2)).
- Declare the official laundry integration category, resolving the HACS metadata
  warning ([#25](https://github.com/Zuz666/uplus-reborn/issues/25)).
- Fix the README logo URL used by HACS. The previously published 1.0.0 tag and
  assets remain unchanged ([#16](https://github.com/Zuz666/uplus-reborn/issues/16)).

### Maintenance

- Update the current isolated Core verification baseline to **2026.10.2**, Ruff
  to 0.17.1 and pinned release/CI actions. Verify named artifact handoffs and
  committed-byte integrity; retain the minimum Core dependency pins.

### Compatibility and known limitations

- Minimum Home Assistant Core remains **2026.2.3**. Restart Home Assistant after
  upgrading; do not remove the config entry or re-pair the appliance. Existing
  entity/statistic identities, preferences and saved profiles are preserved.
- Controls remain opt-in. An unknown outcome never authorizes repeating Start.
  The control fixes are verified with isolated Core and synthetic TCP peers;
  this release does not claim a new physical Tub Clean run or a live deployment.
- Authoritative selected spin speed, numeric end delay, original wash duration
  and exact Smart fabric/stain identities remain unavailable where the appliance
  does not report them. Drafts are not substituted for missing readback; a
  retained ordinary-stain code does not prove active treatment.

## [1.0.0] - 2026-10-22

### Added

- Native Home Assistant integration for the Haier HWD120-B1558U dual-drum
  washer-dryer, distributed as a HACS Custom repository, category Integration.
- Local UWT monitoring and explicitly enabled controls for both drums, including
  the exact-target 12 upper and 20 lower programs and source-defined options.
  Physical verification is narrower than software/profile coverage; no other
  appliance model is supported.
- Account-free UDP/DHCP discovery and manual setup. Optional Euro cloud-assisted
  discovery is onboarding-only; runtime is local and credentials are not stored.
- Persistent per-drum program profiles, independent next-run drafts, lifecycle
  controls, shared options and automation actions without a raw-packet service.
- English and Russian native UI, model-specific manual terminology and a public
  bilingual entity-name registry. No custom Lovelace card is required.
- Water/energy consumption sensors with litre/kWh units and long-term-statistics
  support; authoritative appliance reports remain separate from drafts.
- Reproducible Docker verification, committed-byte HACS packaging, guarded manual
  release automation, security reporting and public contribution workflows.

### Compatibility and safety

- Minimum Home Assistant Core: **2026.2.3**. The required current verification
  baseline is **2026.10.1**; moving Core canaries do not redefine support.
- Restart Home Assistant after installation or upgrade. Preserve existing config
  entries, device/entity IDs, user preferences and profiles; do not delete or
  re-pair an existing installation as an upgrade procedure.
- Appliance controls remain opt-in. Start requires physical power/Remote Control
  and valid fresh state/configuration. There is no automatic write retry or
  optimistic success; Cancel does not prove drainage or safe door opening.
- The initial public repository starts with a clean source snapshot, not imported
  development history. Runtime source is MIT; `docs/` is CC BY 4.0 with attribution.

### Known limitations

- Authoritative selected spin speed and numeric end delay are unavailable when
  the appliance does not report them. They are never filled from a next-run draft.
- The main-wash counter does not establish the original configured wash duration.
- A retained ordinary-stain code does not prove that treatment is currently active.
- Exact Smart fabric/stain identities cannot be reconstructed reliably from the
  inspected readback. Source-defined controls do not make those values observable.
- Owner-reported, non-blocking follow-ups: Tub Clean currently exposes fixed
  settings as editable controls ([#1](https://github.com/Zuz666/uplus-reborn/issues/1));
  an upper Tub Clean start can report an unknown outcome although the cycle starts
  ([#2](https://github.com/Zuz666/uplus-reborn/issues/2)). These reports are tracked
  separately; do not repeat a Start merely because its outcome is uncertain.
