# Appliance protocol references

> Copyright (c) 2026 U+ Reborn contributors. Licensed under
> [CC BY 4.0](LICENSE). When sharing this document or adaptations, attribute
> U+ Reborn contributors, link to
> `https://github.com/Zuz666/uplus-reborn`, link to the license, and indicate
> changes.

Protocol specifications are organized by appliance model. A shared brand,
service port or SDK does not establish compatible framing or field semantics.
Only HWD120-B1558U is currently hardware-verified and supported by U+ Reborn.

| Model | Specification | Scope |
| --- | --- | --- |
| HWD120-B1558U | [Legacy UWT/EPP protocol](protocol/hwd120-b1558u.md) | UDP discovery, TCP envelopes, inner framing, dual-drum state/commands and captured examples; Euro U+ 2.6.0 / EPP profile 4.0.3 |

The model specification distinguishes captured bytes, physical observations,
source-defined fields and unresolved meanings. Packet examples omit or redact
identifying information explicitly; source-only formats are not presented as
hardware captures.

For future models, follow the
[model-specific documentation contract](../CONTRIBUTING.md#model-specific-protocol-documentation).
A new reference does not itself establish integration support. No common
cross-model wire specification is claimed until independent model evidence
supports one. Home Assistant behavior belongs in [README](../README.md), not in
these appliance protocol references.
