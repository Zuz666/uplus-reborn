"""Observable grouped Start contracts for the exact target source builders."""

from __future__ import annotations

import struct
from dataclasses import FrozenInstanceError, replace

import pytest

from custom_components.uplus_reborn.protocol import (
    BuilderContext,
    Drum,
    ProtocolError,
    default_intent,
    encode_start,
    fabric_intent,
    intent_capabilities,
    prepare_intent,
    update_intent,
    validate_intent,
)


@pytest.mark.parametrize("drum,group", [("upper", "0001"), ("lower", "0002")])
def test_captured_spin_400_groups_remain_byte_exact(drum: Drum, group: str) -> None:
    """General source support must preserve the captured no-water payloads."""
    intent = update_intent(default_intent(drum, "spin"), spin_rpm=400)
    assert encode_start(intent) == bytes.fromhex(
        f"{group} 1c00 0000 0000 0000 0190 0002 0000 0000 0000 0000 0000"
    )


def test_complete_smart_preparation_preserves_override_error_precedence() -> None:
    """A supplied default is still an override, before recipe lookup or dispatch."""
    with pytest.raises(ProtocolError):
        prepare_intent(
            "upper",
            fabric="not_a_fabric",
            manual_override_error=ProtocolError,
            delay_half_hour_step=0,
        )
    with pytest.raises(ValueError):
        prepare_intent("upper", fabric="A1", enabled_functions=())
    with pytest.raises(ValueError) as invalid_recipe:
        prepare_intent("upper", fabric="not_a_fabric", manual_override_error=ProtocolError)
    assert type(invalid_recipe.value) is ValueError


def test_group_preserves_shared_flags_and_unedited_source_fields() -> None:
    """Source builders retain unrelated state instead of zeroing the tail."""
    context = BuilderContext(
        load_level=8,
        wash_water_level=65535,
        scrubbing_speed=171,
        washing_rotation_time=205,
        washing_stop_time=239,
    )
    assert encode_start(
        default_intent("lower", "cotton"),
        buzzer_enabled=False,
        child_lock=True,
        context=context,
    ) == bytes.fromhex("0002 0100 0001 0408 1e4b 05dc 0202 0000 0000 ffff abcd ef00")


@pytest.mark.parametrize(
    "drum,program,changes,temperature_and_time",
    [
        ("upper", "colored_laundry", {}, 0x001E),
        ("upper", "allergy_care", {}, 0x5A64),
        ("lower", "hygiene", {}, 0x5A82),
        ("upper", "drum_clean", {}, 0x5A37),
        ("lower", "drum_clean", {}, 0x5A4B),
        ("lower", "wool", {"temperature_c": 0}, 0x0019),
        ("upper", "quick_wash", {"temperature_c": 40}, 0x2817),
        ("lower", "bedding", {}, 0x282D),
    ],
)
def test_builder_heating_uses_exact_program_branches(
    drum: Drum, program: str, changes: dict[str, object], temperature_and_time: int
) -> None:
    """Cold fallback, fixed cleaning, wool and quick wash are not interpolated."""
    intent = update_intent(default_intent(drum, program), **changes)
    words = struct.unpack(">12H", encode_start(intent))
    assert words[4] == temperature_and_time


def test_ordinary_options_and_end_time_encode_together() -> None:
    """Source-offered combinations are not blocked by physical coverage."""
    intent = update_intent(
        default_intent("lower", "cotton"),
        enabled_functions=(
            "high_water",
            "easy_iron",
            "intensive",
            "eco",
            "soak",
            "night",
        ),
        delay_half_hour_step=48,
        drying_mode="120_min",
    )
    words = struct.unpack(">12H", encode_start(intent))
    assert (words[1], words[2], words[4], words[7]) == (0x0130, 0x0400, 0x1E9B, 0x006F)
    hot = update_intent(intent, temperature_c=90)
    assert struct.unpack(">12H", encode_start(hot))[4] == 0x5AB4


@pytest.mark.parametrize(
    "mode,drying_and_flags,wash_duration",
    [("30_min", 0x0700, 30), ("auto", 0x0A00, 150), ("120_min", 0x0400, 120)],
)
def test_dry_program_replaces_wash_duration_from_mode(
    mode: str, drying_and_flags: int, wash_duration: int
) -> None:
    """Dry-only duration is not a normal wash/heating calculation."""
    intent = update_intent(default_intent("lower", "dry"), drying_mode=mode)
    words = struct.unpack(">12H", encode_start(intent))
    assert (words[2], words[4], words[5], words[6]) == (
        drying_and_flags,
        wash_duration,
        0,
        0,
    )


def test_refresh_retains_drying_state_despite_fixed_ui_choice() -> None:
    """The source Refresh branch deliberately skips setDownDryProcedure."""
    intent = default_intent("lower", "refresh")
    words = struct.unpack(
        ">12H", encode_start(intent, context=BuilderContext(drying_value=10))
    )
    assert intent.drying_mode == "30_min"
    assert (words[1], words[2], words[4], words[5], words[6]) == (
        0x0C00,
        0x0A00,
        0,
        0,
        0,
    )


@pytest.mark.parametrize(
    "drum,program,changes",
    [
        ("upper", "spin", {"spin_rpm": 1500}),
        ("lower", "spin", {"spin_rpm": 0}),
        ("upper", "spin", {"wash_minutes": 0}),
        ("upper", "allergy_care", {"wash_minutes": 35}),
        ("upper", "baby_care", {"delay_half_hour_step": 2}),
        ("lower", "hygiene", {"delay_half_hour_step": 4}),
        ("lower", "cotton", {"delay_half_hour_step": 5}),
        ("lower", "cotton", {"delay_half_hour_step": True}),
        ("lower", "cotton", {"rinses": True}),
        ("lower", "cotton", {"enabled_functions": ["night"]}),
        ("lower", "cotton", {"enabled_functions": ("night", "night")}),
        ("upper", "spin", {"enabled_functions": ("easy_iron",)}),
        ("lower", "dry", {"enabled_functions": ("night",)}),
        ("lower", "wool", {"drying_mode": "auto"}),
        ("lower", "dry", {"drying_mode": "off"}),
        ("upper", "baby_care", {"stains": ("W1",)}),
    ],
)
def test_unoffered_or_incomplete_edits_reject_atomically(
    drum: Drum, program: str, changes: dict[str, object]
) -> None:
    """A malformed local edit cannot become a partly accepted command."""
    intent = default_intent(drum, program)
    before = encode_start(intent)
    with pytest.raises(ValueError):
        update_intent(intent, **changes)
    assert encode_start(intent) == before


def test_switching_program_discards_options_and_preserves_old_immutable_draft() -> None:
    """Changing the programme never carries an incompatible function onward."""
    old = update_intent(
        default_intent("lower", "cotton"),
        drying_mode="auto",
        enabled_functions=("soak",),
        delay_half_hour_step=48,
    )
    new = update_intent(old, program="wool")
    assert new == default_intent("lower", "wool")
    assert old.drying_mode == "auto"
    with pytest.raises(FrozenInstanceError):
        old.program = "wool"


def test_smart_stain_uses_source_recipe_and_sets_only_smart_flag() -> None:
    """Smart settings can exceed ordinary limits without writing a W category."""
    intent = fabric_intent("upper", "F1", ("W30",))
    assert (intent.program, intent.wash_minutes, intent.temperature_c) == (
        "baby_care",
        40,
        60,
    )
    assert struct.unpack(">12H", encode_start(intent)) == (
        1,
        0x1400,
        2,
        0,
        0x3C4B,
        1000,
        0x0202,
        0,
        0,
        0,
        0,
        0,
    )
    capability = intent_capabilities(intent)
    assert capability.default.wash_minutes == 40
    assert capability.allowed_wash_minutes == ()


def test_multiple_stains_choose_priority_not_independent_scalar_maxima() -> None:
    """Higher priority wins even when a lower-priority row has a longer time."""
    intent = fabric_intent("upper", "B1", ("W11", "W9"))
    assert intent.stains == ("W9", "W11")
    assert (intent.wash_minutes, intent.temperature_c) == (20, 50)
    assert struct.unpack(">12H", encode_start(intent))[4] == 0x322D


@pytest.mark.parametrize(
    "drum,fabric,stains,wash,temperature,encoded",
    [
        ("upper", "A1", ("W30",), 15, 0, 0x0014),
        ("upper", "A2", ("W1",), 0, 30, 0x1E0A),
        ("lower", "A2", ("W30",), 15, 30, 0x1E19),
        ("upper", "B1", ("W12",), 20, 55, 0x3719),
        ("lower", "B1", ("W14",), 35, 55, 0x3728),
    ],
)
def test_smart_material_limits_and_nonstandard_temperature_follow_source(
    drum: Drum,
    fabric: str,
    stains: tuple[str, ...],
    wash: int,
    temperature: int,
    encoded: int,
) -> None:
    """The warning-confirmed path clamps without guessing a 55-degree formula."""
    intent = fabric_intent(drum, fabric, stains)
    assert (intent.wash_minutes, intent.temperature_c) == (wash, temperature)
    assert struct.unpack(">12H", encode_start(intent))[4] == encoded


def test_smart_updates_rebuild_recipe_and_ordinary_reselection_clears_it() -> None:
    """Fabric and stains remain editable; ordinary controls do not bypass them."""
    first = fabric_intent("upper", "F1", ("W30",))
    changed = update_intent(first, fabric="A1")
    assert changed == fabric_intent("upper", "A1")
    stained = update_intent(changed, stains=("W1",))
    assert stained == fabric_intent("upper", "A1", ("W1",))
    assert update_intent(first, program="baby_care") == default_intent(
        "upper", "baby_care"
    )
    assert update_intent(first, fabric=None) == default_intent("upper", "baby_care")
    with pytest.raises(ValueError, match="cannot be edited"):
        update_intent(first, temperature_c=40)
    with pytest.raises(ValueError):
        validate_intent(replace(first, spin_rpm=1200))
    with pytest.raises(ValueError):
        fabric_intent("upper", "F1", ("W1", "W1"))
    with pytest.raises(ValueError):
        fabric_intent("upper", "F1", ("W31",))


@pytest.mark.parametrize(
    "context",
    [
        BuilderContext(load_level=9),
        BuilderContext(wash_water_level=65536),
        BuilderContext(scrubbing_speed=-1),
        BuilderContext(washing_rotation_time=256),
        BuilderContext(washing_stop_time=True),
        BuilderContext(drying_value=11),
    ],
)
def test_preserved_fields_are_validated_before_packing(context: BuilderContext) -> None:
    """Unknown enum values and invalid widths cannot spill into nearby fields."""
    with pytest.raises(ValueError, match="Invalid preserved"):
        encode_start(default_intent("upper", "spin"), context=context)


def test_shared_boolean_flags_reject_numeric_aliases() -> None:
    """Flags cannot silently accept arbitrary truthy values."""
    with pytest.raises(ValueError, match="booleans"):
        encode_start(default_intent("upper", "spin"), buzzer_enabled=1)


@pytest.mark.parametrize(
    "drum,program,stain,wire_value,temperature,duration",
    [
        ("lower", "mix", "blood", 1, 60, 49),
        ("lower", "mix", "grass", 2, 60, 50),
        ("lower", "mix", "wine", 3, 50, 25),
        ("lower", "mix", "juice", 7, 30, 25),
        ("lower", "mix", "soil", 5, 60, 80),
        ("lower", "mix", "milk", 6, 30, 55),
        ("upper", "casual", "blood", 1, 60, 39),
        ("upper", "casual", "wine", 3, 55, 35),
    ],
)
def test_ordinary_stain_encodes_panel_reference_not_smart_priority(
    drum: Drum,
    program: str,
    stain: str,
    wire_value: int,
    temperature: int,
    duration: int,
) -> None:
    """Enum order, heating and per-drum Wine must reproduce independent references."""
    draft = update_intent(default_intent(drum, program), stains=(stain,))
    words = struct.unpack(">12H", encode_start(draft))
    assert words[4] == (temperature << 8) | duration
    assert words[8] == wire_value << 8
    assert words[2] & 2 == 0
    assert draft.fabric is None
    assert draft.wash_minutes is None


def test_ordinary_stain_compatible_option_adjusts_its_complete_stage() -> None:
    """Intensive adds its source allowance without heating the preset twice."""
    draft = update_intent(default_intent("lower", "mix"), stains=("wine",))
    intensive = update_intent(draft, enabled_functions=("intensive",))
    words = struct.unpack(">12H", encode_start(intensive))
    assert words[4] == 0x322D
    assert words[7] == 4
    assert words[8] == 0x0300
    cleared = update_intent(intensive, stains=())
    assert struct.unpack(">12H", encode_start(cleared))[8] == 0
    assert cleared.enabled_functions == ("intensive",)
