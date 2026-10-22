# Changelog

A dated entry is not confirmation of publication. Published versions and assets
are listed in
[GitHub Releases](https://github.com/Zuz666/uplus-reborn/releases).
Unpublished development versions are not historical public releases.

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
