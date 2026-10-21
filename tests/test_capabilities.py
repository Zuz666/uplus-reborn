"""Contracts for the exact HWD120-B1558U capability matrix."""

from __future__ import annotations

import operator

import pytest

from custom_components.uplus_reborn.protocol import (
    DELAY_HALF_HOUR_STEPS,
    DRYING_MODES,
    LOWER_PROGRAM_CAPABILITIES,
    UPPER_PROGRAM_CAPABILITIES,
    fabric_capabilities,
    fabric_capability,
    program_capabilities,
    program_delay_capabilities,
    program_drying_modes,
    program_function_availability,
)


def test_program_matrix_preserves_drum_specific_profile_limits() -> None:
    """Upper and lower hardware limits must not collapse into one generic map."""
    upper_spin = UPPER_PROGRAM_CAPABILITIES["spin"]
    lower_spin = LOWER_PROGRAM_CAPABILITIES["spin"]
    lower_cotton = LOWER_PROGRAM_CAPABILITIES["cotton"]

    assert upper_spin.profile_code == "30500s"
    assert upper_spin.allowed_spin_rpm == (400, 600, 800, 1000, 1200)
    assert lower_spin.allowed_spin_rpm == (400, 600, 800, 1000, 1200, 1500)
    assert lower_cotton.default.temperature_c == 30
    assert lower_cotton.allowed_temperatures_c == (0, 20, 30, 40, 60, 90)
    assert lower_cotton.allowed_wash_minutes == tuple(range(5, 61, 5))


def test_non_applicable_program_fields_remain_absent() -> None:
    """A dry-only profile must not invent wash settings."""
    lower_dry = program_capabilities("lower", "dry")

    assert lower_dry.default.wash_minutes is None
    assert lower_dry.default.temperature_c is None
    assert lower_dry.default.rinses is None
    assert lower_dry.default.spin_rpm is None
    assert not lower_dry.allowed_temperatures_c


def test_auxiliary_capabilities_keep_raw_profile_values() -> None:
    """Drying and delay values are profile coordinates, not guessed durations."""
    assert DRYING_MODES == {
        "off": 0,
        "120_min": 4,
        "90_min": 5,
        "60_min": 6,
        "30_min": 7,
        "auto": 10,
    }
    assert DELAY_HALF_HOUR_STEPS == range(49)


def test_program_function_exclusions_preserve_source_ui_gating() -> None:
    """Individual availability is known without claiming multi-option validity."""
    upper_spin = program_function_availability("upper", "spin")
    lower_wool = program_function_availability("lower", "wool")

    assert {item.name for item in upper_spin.excluded} == {
        "delay",
        "high_water",
        "easy_iron",
        "intensive",
        "eco",
        "soak",
    }
    assert {item.name for item in upper_spin.available} == {"night"}
    assert {item.name for item in lower_wool.excluded} == {
        "drying",
        "high_water",
        "intensive",
        "eco",
        "soak",
    }
    assert {item.name for item in lower_wool.available} == {
        "delay",
        "easy_iron",
        "night",
    }


def test_drying_modes_follow_per_program_source_exclusions() -> None:
    """Global raw coordinates must not become a false Cartesian UI list."""
    assert [mode.name for mode in program_drying_modes("refresh")] == ["30_min"]
    assert [mode.name for mode in program_drying_modes("dry")] == [
        "auto",
        "30_min",
        "60_min",
        "90_min",
        "120_min",
    ]
    assert program_drying_modes("wool") == ()
    assert [
        (mode.profile_code, mode.value) for mode in program_drying_modes("cotton")
    ] == [
        ("305000", 0),
        ("30500a", 10),
        ("305007", 7),
        ("305006", 6),
        ("305005", 5),
        ("305004", 4),
    ]


def test_delay_steps_use_program_end_time_semantics() -> None:
    """UI delay choices start after cycle duration and exclude half hours."""
    baby = program_delay_capabilities("upper", "baby_care")
    hygiene = program_delay_capabilities("lower", "hygiene")

    assert baby.cycle_minutes == 85
    assert baby.available_half_hour_steps == (0, *range(4, 49, 2))
    assert hygiene.cycle_minutes == 161
    assert hygiene.available_half_hour_steps == (0, *range(6, 49, 2))
    assert program_delay_capabilities("upper", "spin").available_half_hour_steps == ()


def test_public_capability_catalog_is_read_only() -> None:
    """Callers cannot mutate the shared capability snapshot."""
    with pytest.raises(TypeError):
        operator.setitem(
            UPPER_PROGRAM_CAPABILITIES,
            "spin",
            UPPER_PROGRAM_CAPABILITIES["spin"],
        )


def test_unknown_program_is_rejected() -> None:
    """Callers cannot silently synthesize an unsupported program."""
    with pytest.raises(ValueError, match="Unsupported upper program"):
        program_capabilities("upper", "cotton")


def test_dry_and_refresh_do_not_inherit_lossy_array_options() -> None:
    """DEX filled-array exclusions include their omitted middle entries."""
    for program in ("dry", "refresh"):
        availability = program_function_availability("lower", program)
        assert {item.name for item in availability.available} == {"drying", "delay"}


def test_fabrics_use_drum_specific_recipes_not_ordinary_limits() -> None:
    """A fabric draft can legitimately exceed its ordinary main-wash limit."""
    upper = fabric_capability("upper", "F1")
    lower = fabric_capability("lower", "F1")
    assert (upper.program, upper.max_wash_minutes) == ("baby_care", 45)
    assert (lower.program, lower.max_wash_minutes) == ("cotton", 120)
    assert upper.default.rinses == 2
    assert program_capabilities("upper", "baby_care").default.rinses == 3
    assert fabric_capability("upper", "A2").max_wash_minutes == 0
    assert fabric_capability("lower", "A2").max_wash_minutes == 15


def test_fabric_choice_cannot_cross_drum_boundaries() -> None:
    """Large furnishings and the upper-only baby item stay on their drum."""
    assert fabric_capability("lower", "D1").program == "cotton"
    assert fabric_capability("upper", "E6").program == "baby_care"
    with pytest.raises(ValueError, match="Unsupported upper fabric"):
        fabric_capability("upper", "D1")
    with pytest.raises(ValueError, match="Unsupported lower fabric"):
        fabric_capability("lower", "E6")
    with pytest.raises(ValueError, match="Unsupported drum"):
        fabric_capabilities("middle")
