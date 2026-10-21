# Changelog

The 1.0.0 candidate is being prepared; a dated entry is not confirmation of
publication. Published versions and assets are listed in
[GitHub Releases](https://github.com/Zuz666/uplus-reborn/releases).
Unpublished development versions are not historical public releases.

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
