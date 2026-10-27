"""Diagnostics for U+ Reborn."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant

from . import UplusRebornConfigEntry
from .const import (
    CONF_ENABLE_VERIFIED_CONTROLS,
    CONF_POLL_INTERVAL,
    MODEL,
    configured_poll_interval,
)
from .exchange_trace import ExchangeTrace
from .protocol import (
    DELAY_HALF_HOUR_STEPS,
    DRYING_MODE_OPTIONS,
    PROGRAM_CAPABILITIES,
    STAIN_CAPABILITIES,
    STAIN_LABEL_COUNT,
    STAIN_PRIORITY_LEVEL_COUNT,
    VERIFIED_STARTS,
    WASH_OPTION_BITS,
    fabric_capabilities,
    ordinary_stain_capability,
    ordinary_stain_choices,
    program_delay_capabilities,
    program_drying_modes,
    program_function_availability,
)

_STATE_PROVENANCE = {
    "layout": "6d01; big-endian words relative to command word; LSB bit indexes",
    "upper": {
        "program_value": "word 1, bits 8..15",
        "phase_value": "word 3, bits 8..15",
        "door_locked": "word 3, bit 5; reported lock, not physical door position",
        "remaining_minutes": "word 4 bits 0..7 hours + word 5 bits 8..15 minutes; absent in standby",
        "cycle_count": "word 7, 16 bits",
        "cycle_active": "word 3, bit 7; source command-active flag, not motion",
        "delay_active": "word 2, bit 2",
        "builder_context": "load 15H, water 23, scrub 21H, rotate/stop 22H/L",
        "reported_settings": {
            "stain_value": "word 18, bits 8..15; reported code may persist after deselection, not proof of active treatment",
        },
    },
    "lower": {
        "program_value": "word 24, bits 8..15",
        "phase_value": "word 26, bits 8..15",
        "door_locked": "word 26, bit 5; reported lock, not physical door position",
        "remaining_minutes": "word 27 bits 0..7 hours + word 28 bits 8..15 minutes; absent in standby",
        "cycle_count": "word 30, 16 bits",
        "cycle_active": "word 26, bit 7; source command-active flag, not motion",
        "delay_active": "word 25, bit 2",
        "builder_context": "load 32H, water 40, scrub 38H, rotate/stop 39H/L, drying 24L",
        "reported_settings": {
            "stain_value": "word 35, bits 8..15; reported code may persist after deselection, not proof of active treatment",
        },
    },
    "shared": {
        "power": "word 3, bit 0",
        "remote_control": "word 3, bit 1",
        "local_lock": "word 3, bit 3",
        "child_lock": "word 2, bit 3; distinct from local_lock",
        "buzzer_enabled": "word 2, bit 8 inverted; 0 means enabled",
        "current_water_raw": "word 8, 16 bits",
        "total_water_raw": "word 9, 32 bits across words 9..10",
        "current_electricity_raw": "word 11, 16 bits",
        "total_electricity_raw": "word 12, 32 bits across words 12..13",
    },
    "unsupported": (
        "Unknown program/phase codes are unsupported, never guessed.",
        "Motion semantics, hint codes and physical door position are not decoded state.",
        "Ordinary cached state excludes complete frames and unassigned words; opt-in exchange_trace may retain original local bytes.",
        "Consumption fields here are raw; this metadata does not establish units or cycle scope.",
    ),
}


def _capability_diagnostics() -> dict[str, Any]:
    """Return capability provenance with explicit source/hardware limits."""
    programs = {
        drum: {
            name: {
                **asdict(capabilities),
                "profile_code": capabilities.profile_code,
                "source_ui_functions": asdict(
                    program_function_availability(drum, name)
                ),
                "source_ui_delay": asdict(program_delay_capabilities(drum, name)),
                "source_ui_drying_modes": (
                    tuple(asdict(mode) for mode in program_drying_modes(name))
                    if drum == "lower"
                    else ()
                ),
                "verified_start_spin_rpm": tuple(
                    verified.spin_rpm
                    for verified in VERIFIED_STARTS
                    if verified.drum == drum and verified.program == name
                ),
            }
            for name, capabilities in drum_programs.items()
        }
        for drum, drum_programs in PROGRAM_CAPABILITIES.items()
    }
    return {
        "evidence": "Exact-target U+ source, owner's manual and targeted panel references",
        "scope": "source-backed controls; observed overrides and coverage listed separately",
        "programs": programs,
        "smart_fabrics": {
            drum: tuple(asdict(item) for item in fabric_capabilities(drum))
            for drum in ("upper", "lower")
        },
        "verified_starts": [asdict(verified) for verified in VERIFIED_STARTS],
        "lower_drying": {
            "raw_modes": tuple(asdict(mode) for mode in DRYING_MODE_OPTIONS),
            "grouped_start_verified": False,
        },
        "delay": {
            "raw_step_minutes": 30,
            "raw_minimum_step": DELAY_HALF_HOUR_STEPS.start,
            "raw_maximum_step": DELAY_HALF_HOUR_STEPS.stop - 1,
            "ui_semantics": "program end time; whole-hour steps after cycle duration",
            "grouped_start_verified": False,
        },
        "wash_options": {
            "group_word_bits": dict(WASH_OPTION_BITS),
            "per_program_combinations_verified": False,
        },
        "stains": {
            "ui_label_count": STAIN_LABEL_COUNT,
            "source_rows": tuple(asdict(stain) for stain in STAIN_CAPABILITIES),
            "priority_level_count": STAIN_PRIORITY_LEVEL_COUNT,
            "priority_is_wire_enum": False,
        },
        "ordinary_stain_evidence": {
            "availability": "manual-supported programs, independent of Smart fabric",
            "enum": "lower Mix six-choice references; upper Casual Blood/Wine",
            "recipe": "five source/heating recipes; paired per-drum Wine stage totals",
            "remaining_coverage": "other program transfers and physical cleaning efficacy",
            "reported_code_proves_activation": False,
        },
        "ordinary_stains": {
            drum: {
                program: tuple(
                    asdict(ordinary_stain_capability(drum, program, stain))
                    for stain in ordinary_stain_choices(drum, program)
                )
                for program in drum_programs
                if ordinary_stain_choices(drum, program)
            }
            for drum, drum_programs in PROGRAM_CAPABILITIES.items()
        },
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: UplusRebornConfigEntry
) -> dict[str, Any]:
    """Return cached state and allowlisted configuration, including no-data states."""
    coordinator = getattr(entry, "runtime_data", None)
    trace = coordinator.trace if coordinator is not None else ExchangeTrace()
    if coordinator is None:
        trace.close()
    snapshot = coordinator.data if coordinator is not None else None
    state = None
    if snapshot is not None:
        state = asdict(snapshot)
        state["observed_at"] = snapshot.observed_at.isoformat()
    loaded = entry.state is ConfigEntryState.LOADED
    health = {
        "entry_state": entry.state.value,
        "loaded": loaded,
        "available": False,
        "freshness": "never_read",
        "last_success": None,
        "age_seconds": None,
        "last_error": None,
        "latest_command": None,
    }
    if coordinator is not None:
        cached = coordinator.health
        health.update(asdict(cached))
        health["last_success"] = (
            cached.last_success.isoformat() if cached.last_success else None
        )
        if cached.last_error is not None:
            health["last_error"]["at"] = cached.last_error.at.isoformat()
            health["last_error"]["context"] = dict(cached.last_error.context)
        if cached.latest_command is not None:
            health["latest_command"]["at"] = cached.latest_command.at.isoformat()
    if not loaded:
        health["available"] = False
        health["freshness"] = (
            "unloaded"
            if entry.state is ConfigEntryState.NOT_LOADED
            else "never_read"
            if health["last_success"] is None
            else "unavailable"
        )
    return {
        # Allowlist rather than chasing credential/identifier key spellings.
        "config": {CONF_PORT: entry.data.get(CONF_PORT)},
        "options": {
            CONF_ENABLE_VERIFIED_CONTROLS: (
                entry.options.get(CONF_ENABLE_VERIFIED_CONTROLS) is True
            ),
            CONF_POLL_INTERVAL: configured_poll_interval(entry.options),
        },
        "control_mode": (
            "source_derived_opt_in"
            if entry.options.get(CONF_ENABLE_VERIFIED_CONTROLS) is True
            else "monitoring_only"
        ),
        "health": health,
        "device": {"model": MODEL, "state": state, "provenance": _STATE_PROVENANCE},
        "staged_intents": {
            drum: (
                asdict(draft)
                if coordinator is not None
                and loaded
                and (draft := coordinator.draft(drum)) is not None
                else None
            )
            for drum in ("upper", "lower")
        },
        "supported_commands": {
            "drums": {
                drum: ("start", "pause", "resume", "cancel", "reset")
                for drum in ("upper", "lower")
            },
            "appliance_options": ("power", "buzzer", "child_lock"),
            "outcome_proof": "fresh action-specific authoritative postcondition",
            "setting_proof_limits": (
                "Spin speed does not echo at the profile coordinate in captured runs.",
                "Exact delayed end-time has no response field; reservation state is observable.",
                "Refresh preserves the reported drying byte, matching the source builder.",
            ),
            "automatic_retry": False,
        },
        "capabilities": _capability_diagnostics(),
        "exchange_trace": trace.export(),
    }
