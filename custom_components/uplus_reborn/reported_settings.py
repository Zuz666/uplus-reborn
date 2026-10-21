"""Current-cycle applicability for authoritative reported settings."""

from __future__ import annotations

from .protocol import (
    PHASE_OPTIONS,
    PROGRAM_CAPABILITIES,
    DeviceSnapshot,
    Drum,
    ProgramSettingsReport,
)


def contextual_report(
    snapshot: DeviceSnapshot, drum: Drum
) -> ProgramSettingsReport | None:
    """Hide retained coordinates outside a known active or pending cycle.

    Pausing clears cycle_active without ending the phase. Standby, however,
    requires an authoritative start or reservation flag to establish context.
    """
    target = snapshot.upper if drum == "upper" else snapshot.lower
    if (
        target.reported_settings is None
        or target.program not in PROGRAM_CAPABILITIES[drum]
        or target.phase not in PHASE_OPTIONS
        or target.phase == "completed"
        or (
            target.phase == "standby"
            and not (target.delay_active or target.cycle_active)
        )
    ):
        return None
    return target.reported_settings
