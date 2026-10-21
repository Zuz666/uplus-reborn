---
name: Maintainer work item
about: Define one implementable and verifiable change.
title: ""
labels: ""
assignees: ""
---

## Problem and evidence

Describe the consumer-visible problem and link sanitized evidence.

## Scope and non-goals

Keep one coherent outcome. Name affected models/interfaces and excluded work.

## Proposed behavior

Describe states, transitions, failure handling and safety constraints.

## Acceptance criteria

- [ ] The scoped behavior is implemented and exercised at the observable interface.
- [ ] Existing identities, preferences and history are preserved, or migration is approved.
- [ ] Missing data and uncertain command outcomes remain explicit.
- [ ] Affected public contracts are updated.

## Test plan

Select relevant behavior tests, Docker/isolated Core, native UI or HACS scenarios.
State why each selected layer proves the change; appliance access is not a normal
prerequisite. Identify reusable evidence and its input/environment binding.

## Dependencies and release impact

- Blocked by / blocks:
- Parent epic, if any:
- Target milestone:
- Breaking change and migration:
- Proposed release notes:

## Triage

Assign one type and priority, and one or two area labels. Project status owns the
workflow state; do not duplicate it with status labels for normal progress.
